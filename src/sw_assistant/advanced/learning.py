from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from ..domain import utc_now
from ..library.repository import LibraryRepository


@dataclass(frozen=True, slots=True)
class DemonstrationRecipe:
    id: str
    name: str
    document_type: str
    constraints: dict[str, Any]
    steps: tuple[dict[str, Any], ...]
    risk: str
    review_state: str
    success_count: int = 0
    failure_count: int = 0


class DemonstrationService:
    """Records observable actions; recipes remain drafts until explicit approval."""

    def __init__(self, repository: LibraryRepository) -> None:
        self.repository = repository

    def start(
        self,
        document_type: str,
        *,
        document_path: str = "",
        context: Mapping[str, Any] | None = None,
    ) -> str:
        normalized = str(document_type).strip().casefold()
        if normalized not in {"part", "assembly", "drawing"}:
            raise ValueError("示教文档类型必须是 part、assembly 或 drawing。")
        session_id = uuid.uuid4().hex
        now = utc_now()
        with self.repository.connection() as db:
            db.execute(
                "INSERT INTO demonstration_sessions(id,document_type,document_path,context_json,"
                "state,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                (
                    session_id,
                    normalized,
                    str(document_path),
                    self._encode(context or {}),
                    "recording",
                    now,
                    now,
                ),
            )
        return session_id

    def record_action(
        self,
        session_id: str,
        *,
        command_id: int,
        command_name: str = "",
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        result: Mapping[str, Any] | None = None,
    ) -> int:
        now = utc_now()
        with self.repository.connection() as db:
            session = db.execute(
                "SELECT state FROM demonstration_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if session is None or str(session[0]) != "recording":
                raise RuntimeError("示教会话不存在或已经结束。")
            sequence = int(
                db.execute(
                    "SELECT COALESCE(MAX(sequence),-1)+1 FROM demonstration_actions WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
            )
            db.execute(
                "INSERT INTO demonstration_actions(session_id,sequence,command_id,command_name,"
                "before_json,after_json,result_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    sequence,
                    int(command_id),
                    str(command_name),
                    self._encode(before or {}),
                    self._encode(after or {}),
                    self._encode(result or {}),
                    now,
                ),
            )
            db.execute(
                "UPDATE demonstration_sessions SET updated_at=? WHERE id=?",
                (now, session_id),
            )
        return sequence

    def finish(self, session_id: str) -> None:
        with self.repository.connection() as db:
            count = int(
                db.execute(
                    "SELECT COUNT(*) FROM demonstration_actions WHERE session_id=?",
                    (session_id,),
                ).fetchone()[0]
            )
            if count == 0:
                raise RuntimeError("没有记录到任何操作，不能结束示教。")
            cursor = db.execute(
                "UPDATE demonstration_sessions SET state='review',updated_at=? "
                "WHERE id=? AND state='recording'",
                (utc_now(), session_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("示教会话不存在或已经结束。")

    def compile_recipe(self, session_id: str, name: str) -> DemonstrationRecipe:
        normalized_name = str(name).strip()
        if not normalized_name:
            raise ValueError("配方名称不能为空。")
        with self.repository.connection() as db:
            session = db.execute(
                "SELECT * FROM demonstration_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if session is None or str(session["state"]) != "review":
                raise RuntimeError("示教会话尚未进入审阅状态。")
            actions = db.execute(
                "SELECT * FROM demonstration_actions WHERE session_id=? ORDER BY sequence",
                (session_id,),
            ).fetchall()
        context = self._decode(session["context_json"])
        constraints = {
            "document_type": str(session["document_type"]),
            "selection_types": list(context.get("selection_types") or []),
            "requires_manual_target_review": True,
        }
        steps = tuple(
            {
                "sequence": int(row["sequence"]),
                "command_id": int(row["command_id"]),
                "command_name": str(row["command_name"]),
                "context_delta": self._delta(
                    self._decode(row["before_json"]), self._decode(row["after_json"])
                ),
                "observed_result": self._decode(row["result_json"]),
            }
            for row in actions
        )
        recipe_id = uuid.uuid4().hex
        now = utc_now()
        with self.repository.connection() as db:
            db.execute(
                "INSERT INTO recipes(id,name,document_type,constraints_json,steps_json,risk,"
                "review_state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    recipe_id,
                    normalized_name,
                    str(session["document_type"]),
                    self._encode(constraints),
                    json.dumps(steps, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    "review",
                    "draft",
                    now,
                    now,
                ),
            )
        recipe = self.recipe(recipe_id)
        if recipe is None:
            raise RuntimeError("示教配方保存后无法读回。")
        return recipe

    def approve(self, recipe_id: str) -> DemonstrationRecipe:
        with self.repository.connection() as db:
            cursor = db.execute(
                "UPDATE recipes SET review_state='approved',updated_at=? "
                "WHERE id=? AND review_state='draft'",
                (utc_now(), recipe_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("配方不存在或已经审阅。")
        recipe = self.recipe(recipe_id)
        if recipe is None:
            raise RuntimeError("配方批准后无法读回。")
        return recipe

    def recipe(self, recipe_id: str) -> DemonstrationRecipe | None:
        with self.repository.connection() as db:
            row = db.execute("SELECT * FROM recipes WHERE id=?", (recipe_id,)).fetchone()
        if row is None:
            return None
        steps = json.loads(str(row["steps_json"]))
        return DemonstrationRecipe(
            str(row["id"]),
            str(row["name"]),
            str(row["document_type"]),
            self._decode(row["constraints_json"]),
            tuple(dict(item) for item in steps),
            str(row["risk"]),
            str(row["review_state"]),
            int(row["success_count"]),
            int(row["failure_count"]),
        )

    def prepare_replay(self, recipe_id: str, context: Mapping[str, Any]) -> dict[str, Any]:
        """Build a human-confirmed replay guide without reusing topology blindly."""

        recipe = self.recipe(recipe_id)
        if recipe is None:
            raise RuntimeError("示教配方不存在。")
        usable, reason = self.applicable(recipe, context)
        if not usable:
            raise RuntimeError(reason)
        return {
            "recipe_id": recipe.id,
            "name": recipe.name,
            "risk": recipe.risk,
            "mode": "guided",
            "manual_confirmation_required": True,
            "reason": reason,
            "steps": [
                {
                    "sequence": int(step.get("sequence") or index),
                    "command_id": int(step.get("command_id") or 0),
                    "command_name": str(step.get("command_name") or ""),
                    "expected_delta": dict(step.get("context_delta") or {}),
                    "target_confirmation_required": True,
                    "status": "pending",
                }
                for index, step in enumerate(recipe.steps)
            ],
        }

    def record_replay_result(self, recipe_id: str, *, succeeded: bool) -> DemonstrationRecipe:
        column = "success_count" if succeeded else "failure_count"
        with self.repository.connection() as db:
            cursor = db.execute(
                f"UPDATE recipes SET {column}={column}+1,updated_at=? "
                "WHERE id=? AND review_state='approved'",
                (utc_now(), recipe_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("配方不存在或尚未批准，不能记录回放结果。")
        recipe = self.recipe(recipe_id)
        if recipe is None:
            raise RuntimeError("配方回放结果保存后无法读回。")
        return recipe

    @staticmethod
    def applicable(recipe: DemonstrationRecipe, context: Mapping[str, Any]) -> tuple[bool, str]:
        if recipe.review_state != "approved":
            return False, "配方尚未通过人工审阅。"
        if str(context.get("document_type") or "").casefold() != recipe.document_type:
            return False, "活动文档类型与配方不匹配。"
        expected = set(recipe.constraints.get("selection_types") or [])
        actual = set(context.get("selection_types") or [])
        if expected and not expected.issubset(actual):
            return False, "当前选择类型不满足配方约束。"
        return True, "配方上下文匹配；执行前仍需确认目标引用。"

    @staticmethod
    def _delta(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
        keys = set(before) | set(after)
        return {
            key: {"before": before.get(key), "after": after.get(key)}
            for key in sorted(keys)
            if before.get(key) != after.get(key)
        }

    @staticmethod
    def _encode(value: Mapping[str, Any]) -> str:
        return json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _decode(value: Any) -> dict[str, Any]:
        try:
            decoded = json.loads(str(value or "{}"))
        except json.JSONDecodeError:
            return {}
        return dict(decoded) if isinstance(decoded, dict) else {}
