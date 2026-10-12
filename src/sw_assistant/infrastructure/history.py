from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..domain import Plan, utc_now


class HistoryStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS plan_history (
                    id TEXT PRIMARY KEY,
                    prompt TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def _connection(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def save_plan(self, prompt: str, plan: Plan) -> None:
        now = utc_now()
        with self._connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO plan_history(id,prompt,plan_json,state,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (
                    plan.id,
                    prompt,
                    json.dumps(plan.to_dict(), ensure_ascii=False, separators=(",", ":")),
                    "ready",
                    now,
                    now,
                ),
            )

    def update(self, plan_id: str, state: str, error: str = "") -> None:
        with self._connection() as db:
            db.execute(
                "UPDATE plan_history SET state=?,error=?,updated_at=? WHERE id=?",
                (state, error[:4000], utc_now(), plan_id),
            )

    def update_plan(self, plan: Plan) -> None:
        with self._connection() as db:
            db.execute(
                "UPDATE plan_history SET plan_json=?,updated_at=? WHERE id=?",
                (
                    json.dumps(plan.to_dict(), ensure_ascii=False, separators=(",", ":")),
                    utc_now(),
                    plan.id,
                ),
            )

    def save_failure(
        self,
        prompt: str,
        error: str,
        *,
        plan: Plan | Mapping[str, Any] | None = None,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> str:
        record_id = "planning-failed-" + uuid.uuid4().hex
        now = utc_now()
        payload: dict[str, Any] = {}
        if isinstance(plan, Plan):
            payload["plan"] = plan.to_dict()
        elif isinstance(plan, Mapping):
            payload["plan"] = dict(plan)
        if diagnostics:
            payload["diagnostics"] = dict(diagnostics)
        with self._connection() as db:
            db.execute(
                "INSERT INTO plan_history(id,prompt,plan_json,state,error,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    record_id,
                    prompt,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    "failed",
                    error[:4000],
                    now,
                    now,
                ),
            )
        return record_id

    def import_legacy(
        self,
        *,
        legacy_id: int,
        prompt: str,
        design_plan: str,
        state: str,
        error: str,
        created_at: str,
        updated_at: str,
    ) -> bool:
        plan_id = f"legacy-{legacy_id}"
        payload = {
            "legacy": True,
            "legacy_id": legacy_id,
            "design_plan": design_plan,
        }
        with self._connection() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO plan_history("
                "id,prompt,plan_json,state,error,created_at,updated_at"
                ") VALUES (?,?,?,?,?,?,?)",
                (
                    plan_id,
                    prompt,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    state,
                    error[:4000],
                    created_at or utc_now(),
                    updated_at or created_at or utc_now(),
                ),
            )
            return cursor.rowcount > 0

    def recent(self, limit: int = 100) -> list[dict]:
        with self._connection() as db:
            db.row_factory = sqlite3.Row
            rows = db.execute(
                "SELECT id,prompt,state,error,created_at,updated_at FROM plan_history "
                "ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]
