from __future__ import annotations

import csv
import hashlib
import math
import os
import shutil
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from ..domain import ToolCall, ToolError
from ..solidworks.client import ToolWorker


@dataclass(frozen=True, slots=True)
class FamilyVariant:
    name: str
    dimensions_mm: dict[str, float]
    properties: dict[str, str]


@dataclass(frozen=True, slots=True)
class ProductFamilySpec:
    source: str
    destination: str
    variants: tuple[FamilyVariant, ...]
    use_design_table: bool = False

    def validate(self, *, require_source: bool = True) -> None:
        source = Path(self.source).resolve(strict=False)
        destination = Path(self.destination).resolve(strict=False)
        if source.suffix.casefold() != ".sldprt":
            raise ValueError("产品族源文件必须是 SLDPRT。")
        if destination.suffix.casefold() != ".sldprt":
            raise ValueError("产品族输出文件必须是 SLDPRT。")
        if source == destination:
            raise ValueError("产品族输出不能覆盖源文件。")
        if require_source and not source.is_file():
            raise ValueError("产品族源文件不存在。")
        if not 1 <= len(self.variants) <= 1000:
            raise ValueError("产品族配置数量必须位于 1 到 1000 之间。")
        names: set[str] = set()
        dimension_set: set[str] | None = None
        for variant in self.variants:
            name = variant.name.strip()
            if not name or any(character in name for character in "\\/:*?\"<>|"):
                raise ValueError(f"配置名称无效：{variant.name!r}")
            folded = name.casefold()
            if folded in names:
                raise ValueError(f"配置名称重复：{name}")
            names.add(folded)
            if not variant.dimensions_mm:
                raise ValueError(f"配置 {name} 没有尺寸参数。")
            current = {key.strip() for key in variant.dimensions_mm if key.strip()}
            if len(current) != len(variant.dimensions_mm):
                raise ValueError(f"配置 {name} 包含空尺寸名称。")
            if dimension_set is None:
                dimension_set = current
            elif current != dimension_set:
                raise ValueError("所有产品族配置必须提供相同的尺寸列。")
            for dimension, raw_value in variant.dimensions_mm.items():
                value = float(raw_value)
                if not math.isfinite(value) or value <= 0 or value > 1_000_000:
                    raise ValueError(f"配置 {name} 的尺寸 {dimension} 超出安全范围。")


class ProductFamilyAdapter(Protocol):
    def generate_product_family(
        self,
        spec: ProductFamilySpec,
        *,
        cancel: threading.Event,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> dict: ...


class ProductFamilyService:
    def __init__(self, adapter: ProductFamilyAdapter) -> None:
        self.adapter = adapter

    def generate(
        self,
        spec: ProductFamilySpec,
        *,
        cancel: threading.Event | None = None,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> dict:
        spec.validate()
        cancellation = cancel or threading.Event()
        if cancellation.is_set():
            raise InterruptedError("产品族生成已取消。")
        result = self.adapter.generate_product_family(
            spec, cancel=cancellation, progress=progress
        )
        created = {str(name).casefold() for name in result.get("configurations", [])}
        expected = {item.name.casefold() for item in spec.variants}
        if created != expected:
            missing = sorted(expected - created)
            extra = sorted(created - expected)
            raise RuntimeError(f"产品族验证不一致；缺少={missing}，多出={extra}。")
        return result

    @staticmethod
    def from_csv(
        source: str,
        destination: str,
        csv_path: str | Path,
        *,
        use_design_table: bool = False,
    ) -> ProductFamilySpec:
        path = Path(csv_path)
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            headers = list(reader.fieldnames or [])
            if "configuration" not in headers:
                raise ValueError("CSV 必须包含 configuration 列。")
            dimension_headers = [item for item in headers if item.startswith("dimension:")]
            property_headers = [item for item in headers if item.startswith("property:")]
            if not dimension_headers:
                raise ValueError("CSV 至少需要一个 dimension:<名称> 列。")
            variants: list[FamilyVariant] = []
            for row_number, row in enumerate(reader, 2):
                try:
                    dimensions = {
                        header.split(":", 1)[1]: float(str(row.get(header) or ""))
                        for header in dimension_headers
                    }
                except ValueError as exc:
                    raise ValueError(f"CSV 第 {row_number} 行包含无效尺寸。") from exc
                properties = {
                    header.split(":", 1)[1]: str(row.get(header) or "")
                    for header in property_headers
                }
                variants.append(
                    FamilyVariant(str(row.get("configuration") or ""), dimensions, properties)
                )
        spec = ProductFamilySpec(source, destination, tuple(variants), use_design_table)
        spec.validate()
        return spec


def variants_from_rows(rows: Iterable[dict]) -> tuple[FamilyVariant, ...]:
    return tuple(
        FamilyVariant(
            str(row.get("name") or ""),
            {str(key): float(value) for key, value in dict(row.get("dimensions_mm") or {}).items()},
            {str(key): str(value) for key, value in dict(row.get("properties") or {}).items()},
        )
        for row in rows
    )


class WorkerProductFamilyAdapter:
    """Generate a product-family copy without ever writing to the source part."""

    def __init__(self, worker: ToolWorker) -> None:
        self.worker = worker

    def generate_product_family(
        self,
        spec: ProductFamilySpec,
        *,
        cancel: threading.Event,
        progress: Callable[[int, int, str], None] | None = None,
    ) -> dict[str, Any]:
        spec.validate()
        source = Path(spec.source).resolve(strict=True)
        destination = Path(spec.destination).resolve(strict=False)
        if destination.exists():
            raise ToolError("产品族目标文件已存在，安全生成不会覆盖文件。")
        if not destination.parent.is_dir():
            raise ToolError("产品族目标目录不存在。")
        source_hash = self._sha256(source, cancel)
        staging = destination.with_name(
            f".{destination.stem}.sw-family-{uuid.uuid4().hex}{destination.suffix}"
        )
        try:
            self._copy(source, staging, cancel)
            self._progress(progress, 0, len(spec.variants), "已创建隔离的产品族临时副本")
            payload = {
                "path": str(staging),
                "variants": [
                    {
                        "name": variant.name,
                        "dimensions_mm": dict(variant.dimensions_mm),
                        "properties": dict(variant.properties),
                    }
                    for variant in spec.variants
                ],
                "use_design_table": bool(spec.use_design_table),
            }
            self._raise_if_cancelled(cancel)
            result = self.worker.call(
                ToolCall(
                    "product_family.generate",
                    payload,
                    "在隔离副本中创建并校验产品族配置",
                ),
                max(300, len(spec.variants) * 20),
            )
            if not result.ok:
                raise ToolError(result.message or "SolidWorks 产品族生成失败。")
            self._raise_if_cancelled(cancel)
            data = dict(result.data or {})
            configurations = [str(item) for item in data.get("configurations", [])]
            expected = {item.name.casefold() for item in spec.variants}
            if {item.casefold() for item in configurations} != expected:
                raise ToolError("SolidWorks 返回的产品族配置清单与请求不一致。")
            if self._sha256(source, cancel) != source_hash:
                raise ToolError("产品族生成期间源文件发生变化，结果未发布。")
            generated_hash = self._sha256(staging, cancel)
            self._raise_if_cancelled(cancel)
            os.link(staging, destination)
            self._progress(
                progress,
                len(spec.variants),
                len(spec.variants),
                "产品族已经校验并发布",
            )
            data.update(
                {
                    "source": str(source),
                    "destination": str(destination),
                    "source_sha256": source_hash,
                    "destination_sha256": generated_hash,
                    "strategy": "configuration_copy",
                }
            )
            return data
        finally:
            staging.unlink(missing_ok=True)

    @staticmethod
    def _raise_if_cancelled(cancel: threading.Event) -> None:
        if cancel.is_set():
            raise InterruptedError("产品族生成已取消。")

    @classmethod
    def _sha256(cls, path: Path, cancel: threading.Event) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                cls._raise_if_cancelled(cancel)
                digest.update(block)
        return digest.hexdigest()

    @classmethod
    def _copy(cls, source: Path, destination: Path, cancel: threading.Event) -> None:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with source.open("rb") as input_stream, os.fdopen(descriptor, "wb") as output_stream:
                while block := input_stream.read(1024 * 1024):
                    cls._raise_if_cancelled(cancel)
                    output_stream.write(block)
                output_stream.flush()
                os.fsync(output_stream.fileno())
            shutil.copystat(source, destination)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            destination.unlink(missing_ok=True)
            raise

    @staticmethod
    def _progress(
        callback: Callable[[int, int, str], None] | None,
        completed: int,
        total: int,
        message: str,
    ) -> None:
        if callback:
            callback(completed, total, message)
