from __future__ import annotations

import json
import math
import re
import threading
from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..domain import utc_now
from ..library.repository import DocumentRecord, LibraryRepository


INDEX_VERSION = 1
_LATIN = re.compile(r"[a-z0-9_.-]+", re.IGNORECASE)
_CJK = re.compile(r"[\u3400-\u9fff]+")


@dataclass(frozen=True, slots=True)
class SemanticHit:
    document_id: int
    name: str
    path: str
    document_type: str
    score: float
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "name": self.name,
            "path": self.path,
            "document_type": self.document_type,
            "score": self.score,
            "reasons": list(self.reasons),
        }


class LocalSemanticIndex:
    FIELD_WEIGHTS = {
        "name": 6.0,
        "tags": 5.0,
        "material": 4.0,
        "properties": 3.5,
        "configurations": 3.0,
        "features": 2.5,
        "dimensions": 2.0,
        "document_type": 1.5,
        "path": 1.0,
    }

    def __init__(self, repository: LibraryRepository) -> None:
        self.repository = repository

    def index_document(
        self,
        record: DocumentRecord,
        manifest: Mapping[str, Any],
        *,
        tags: Iterable[str] = (),
    ) -> None:
        fields = self._fields(record, manifest, tags)
        tokens = {name: self.tokenize(text) for name, text in fields.items()}
        search_text = "\n".join(value for value in fields.values() if value)
        with self.repository.connection() as db:
            db.execute(
                "INSERT INTO semantic_documents(document_id,search_text,field_tokens_json,"
                "index_version,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(document_id) DO UPDATE SET search_text=excluded.search_text,"
                "field_tokens_json=excluded.field_tokens_json,index_version=excluded.index_version,"
                "updated_at=excluded.updated_at",
                (
                    record.id,
                    search_text,
                    json.dumps(tokens, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    INDEX_VERSION,
                    utc_now(),
                ),
            )

    def rebuild(self, *, cancel: threading.Event | None = None) -> dict[str, int]:
        cancellation = cancel or threading.Event()
        indexed = skipped = 0
        records = self.repository.analyzable_documents()
        known = {record.id for record in records}
        for record in records:
            if cancellation.is_set():
                raise InterruptedError("语义索引重建已取消。")
            manifest = self.repository.manifest(record.id)
            if not manifest:
                skipped += 1
                continue
            self.index_document(record, manifest, tags=self.repository.tags(record.id))
            indexed += 1
        with self.repository.connection() as db:
            if known:
                placeholders = ",".join("?" for _ in known)
                db.execute(
                    f"DELETE FROM semantic_documents WHERE document_id NOT IN ({placeholders})",
                    tuple(sorted(known)),
                )
            else:
                db.execute("DELETE FROM semantic_documents")
        return {"indexed": indexed, "skipped": skipped, "total": len(records)}

    def search(self, query: str, *, limit: int = 20) -> list[SemanticHit]:
        normalized = str(query).strip()
        if not normalized:
            return []
        query_tokens = self.tokenize(normalized)
        if not query_tokens:
            return []
        with self.repository.connection() as db:
            rows = db.execute(
                "SELECT s.document_id,s.search_text,s.field_tokens_json,d.name,d.path,d.document_type "
                "FROM semantic_documents s JOIN documents d ON d.id=s.document_id "
                "WHERE d.missing=0 AND s.index_version=?",
                (INDEX_VERSION,),
            ).fetchall()
        documents: list[tuple[Any, dict[str, list[str]]]] = []
        for row in rows:
            try:
                fields = json.loads(str(row["field_tokens_json"]))
            except json.JSONDecodeError:
                continue
            if isinstance(fields, dict):
                documents.append((row, fields))
        if not documents:
            return []
        document_frequency: Counter[str] = Counter()
        for _row, fields in documents:
            present = {token for values in fields.values() for token in values}
            document_frequency.update(present)
        total = len(documents)
        hits: list[SemanticHit] = []
        for row, fields in documents:
            score = 0.0
            reasons: list[str] = []
            for field, weight in self.FIELD_WEIGHTS.items():
                counts = Counter(fields.get(field, []))
                matched: list[str] = []
                for token in query_tokens:
                    frequency = counts[token]
                    if not frequency:
                        continue
                    inverse = math.log((total + 1) / (document_frequency[token] + 0.5)) + 1
                    score += weight * inverse * (1 + math.log(frequency))
                    matched.append(token)
                if matched:
                    reasons.append(f"{self._field_label(field)}匹配：{', '.join(sorted(set(matched))) }")
            if normalized.casefold() in str(row["search_text"]).casefold():
                score += 8.0
                reasons.insert(0, "包含完整查询短语")
            if score > 0:
                hits.append(
                    SemanticHit(
                        int(row["document_id"]),
                        str(row["name"]),
                        str(row["path"]),
                        str(row["document_type"]),
                        round(score, 4),
                        tuple(reasons[:5]),
                    )
                )
        hits.sort(key=lambda item: (-item.score, item.name.casefold(), item.path.casefold()))
        return hits[: max(1, min(int(limit), 200))]

    @staticmethod
    def tokenize(value: str) -> list[str]:
        text = str(value).casefold()
        tokens = _LATIN.findall(text)
        for run in _CJK.findall(text):
            tokens.append(run)
            if len(run) > 1:
                tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
        return tokens

    @classmethod
    def _fields(
        cls,
        record: DocumentRecord,
        manifest: Mapping[str, Any],
        tags: Iterable[str],
    ) -> dict[str, str]:
        return {
            "name": record.name,
            "path": record.path,
            "document_type": record.document_type,
            "tags": " ".join(str(item) for item in tags),
            "material": cls._text(manifest.get("material")),
            "properties": cls._text(
                [manifest.get("custom_properties"), manifest.get("configuration_properties")]
            ),
            "configurations": cls._text(manifest.get("configurations")),
            "features": cls._text(manifest.get("features")),
            "dimensions": cls._text(manifest.get("dimensions")),
        }

    @classmethod
    def _text(cls, value: Any) -> str:
        output: list[str] = []
        if isinstance(value, Mapping):
            for key, item in value.items():
                output.append(str(key))
                output.append(cls._text(item))
        elif isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            output.extend(cls._text(item) for item in value)
        elif value is not None:
            output.append(str(value))
        return " ".join(item for item in output if item)

    @staticmethod
    def _field_label(field: str) -> str:
        return {
            "name": "名称",
            "tags": "标签",
            "material": "材料",
            "properties": "属性",
            "configurations": "配置",
            "features": "特征",
            "dimensions": "尺寸",
            "document_type": "文档类型",
            "path": "路径",
        }.get(field, field)
