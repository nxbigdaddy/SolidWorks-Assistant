"""One-time, local, read-only import from the preceding product data directory."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path


def import_previous_data(source: Path, destination: Path) -> dict:
    destination.mkdir(parents=True, exist_ok=True)
    marker = destination / "v4-import.json"
    if source.resolve() == destination.resolve():
        return {"imported": [], "errors": []}
    previous: dict = {}
    if marker.exists():
        try:
            value = json.loads(marker.read_text("utf-8"))
            if isinstance(value, dict):
                previous = value
        except (OSError, ValueError):
            # An interrupted/invalid marker must not disable safe import forever.
            pass
        if previous and previous.get("errors") == []:
            return {"imported": [], "errors": []}
    previous_errors = previous.get("errors", [])
    if not isinstance(previous_errors, list):
        previous_errors = []
    failed_names = {str(error).partition(":")[0] for error in previous_errors}
    report: dict[str, list[str]] = {"imported": [], "errors": []}
    for name in ("settings.json", "library.db", "history.db"):
        original, target = source / name, destination / name
        if target.exists():
            if name in failed_names:
                # The application or user may have created this file after a
                # failed first import. Preserve it and retain the unresolved
                # status rather than claiming the old data was imported.
                report["errors"].append(f"{name}: ExistingTargetPreserved")
            continue
        if not original.is_file():
            if name in failed_names:
                report["errors"].append(f"{name}: SourceUnavailable")
            continue
        temporary = destination / (name + "." + uuid.uuid4().hex + ".tmp")
        try:
            if original.suffix == ".db":
                deadline = time.monotonic() + 10

                def progress(_status: int, _remaining: int, _total: int) -> None:
                    if time.monotonic() > deadline:
                        raise TimeoutError("旧数据库繁忙，未能在限定时间内复制")

                with closing(sqlite3.connect(original.resolve().as_uri() + "?mode=ro", uri=True)) as src:
                    with closing(sqlite3.connect(temporary)) as dst:
                        src.backup(dst, pages=128, progress=progress, sleep=0.05)
            else:
                # Preserve the DPAPI ciphertext; never decrypt or print it here.
                shutil.copyfile(original, temporary)
            temporary.rename(target)
            report["imported"].append(name)
        except Exception as exc:
            report["errors"].append(f"{name}: {type(exc).__name__}")
        finally:
            temporary.unlink(missing_ok=True)
    previous_imported = previous.get("imported", [])
    if not isinstance(previous_imported, list):
        previous_imported = []
    saved_report = {
        "imported": sorted(set(report["imported"]) | {
            name for name in previous_imported
            if isinstance(name, str) and name in {"settings.json", "library.db", "history.db"}
        }),
        "errors": report["errors"],
        "previous_errors": [str(error)[:200] for error in previous_errors[-20:]],
    }
    temporary_marker = destination / f"v4-import.{uuid.uuid4().hex}.tmp"
    try:
        temporary_marker.write_text(
            json.dumps(saved_report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary_marker, marker)
    finally:
        temporary_marker.unlink(missing_ok=True)
    return report
