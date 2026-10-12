from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from ..domain import utc_now
from ..library.repository import LibraryRepository


TERMINAL_JOB_STATES = frozenset({"succeeded", "failed", "cancelled"})
TERMINAL_ITEM_STATES = frozenset({"succeeded", "failed", "cancelled", "skipped"})


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: str
    kind: str
    state: str
    total: int
    processed: int
    succeeded: int
    failed: int
    message: str
    input: dict[str, Any]
    output: dict[str, Any]
    error_code: str
    cancel_requested: bool
    pause_requested: bool
    lease_owner: str
    lease_expires_at: str
    heartbeat_at: str
    attempt: int
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class JobItemRecord:
    id: int
    job_id: str
    sequence: int
    idempotency_key: str
    state: str
    input: dict[str, Any]
    output: dict[str, Any]
    error_code: str
    message: str
    attempt: int
    started_at: str
    finished_at: str


class PersistentJobStore:
    def __init__(self, repository: LibraryRepository) -> None:
        self.repository = repository

    def create(
        self,
        kind: str,
        items: Iterable[Mapping[str, Any]],
        *,
        input: Mapping[str, Any] | None = None,
        job_id: str | None = None,
    ) -> JobRecord:
        normalized_kind = str(kind).strip()
        if not normalized_kind:
            raise ValueError("批任务类型不能为空。")
        normalized_items = [dict(item) for item in items]
        if not normalized_items:
            raise ValueError("批任务至少需要一个任务项。")
        if len(normalized_items) > 100_000:
            raise ValueError("单个批任务不能超过 100000 项。")
        identifier = job_id or uuid.uuid4().hex
        now = utc_now()
        keys: set[str] = set()
        with self.repository.connection() as db:
            db.execute(
                "INSERT INTO jobs(id,kind,state,total,input_json,created_at,updated_at) "
                "VALUES (?,?,?, ?,?,?,?)",
                (
                    identifier,
                    normalized_kind,
                    "queued",
                    len(normalized_items),
                    self._encode(input or {}),
                    now,
                    now,
                ),
            )
            for sequence, payload in enumerate(normalized_items):
                key = str(payload.pop("idempotency_key", "") or sequence).strip()
                if not key or key in keys:
                    raise ValueError(f"批任务项幂等键无效或重复：{key!r}")
                keys.add(key)
                db.execute(
                    "INSERT INTO job_items(job_id,sequence,idempotency_key,input_json,"
                    "created_at,updated_at) VALUES (?,?,?,?,?,?)",
                    (identifier, sequence, key, self._encode(payload), now, now),
                )
        record = self.get(identifier)
        if record is None:
            raise RuntimeError("批任务创建后无法读回。")
        return record

    def get(self, job_id: str) -> JobRecord | None:
        with self.repository.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def list_jobs(self, *, limit: int = 200, state: str = "") -> list[JobRecord]:
        sql = "SELECT * FROM jobs"
        params: list[object] = []
        if state:
            sql += " WHERE state=?"
            params.append(state)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 1000)))
        with self.repository.connection() as db:
            rows = db.execute(sql, params).fetchall()
        return [self._job(row) for row in rows]

    def items(self, job_id: str) -> list[JobItemRecord]:
        with self.repository.connection() as db:
            rows = db.execute(
                "SELECT * FROM job_items WHERE job_id=? ORDER BY sequence", (job_id,)
            ).fetchall()
        return [self._item(row) for row in rows]

    def recover_expired(self, *, now: datetime | None = None) -> int:
        instant = (now or datetime.now(timezone.utc)).isoformat()
        with self.repository.connection() as db:
            rows = db.execute(
                "SELECT id FROM jobs WHERE state='running' AND lease_expires_at<>'' "
                "AND lease_expires_at<?",
                (instant,),
            ).fetchall()
            identifiers = [str(row[0]) for row in rows]
            for identifier in identifiers:
                db.execute(
                    "UPDATE job_items SET state='pending',started_at='',updated_at=? "
                    "WHERE job_id=? AND state='running'",
                    (instant, identifier),
                )
                db.execute(
                    "UPDATE jobs SET state='queued',lease_owner='',lease_expires_at='',"
                    "message='执行进程中断，任务已回收到队列。',updated_at=? WHERE id=?",
                    (instant, identifier),
                )
        return len(identifiers)

    def claim(
        self,
        owner: str,
        *,
        lease_seconds: int = 30,
        kinds: Iterable[str] | None = None,
    ) -> JobRecord | None:
        normalized_owner = str(owner).strip()
        if not normalized_owner:
            raise ValueError("任务执行器标识不能为空。")
        if not 5 <= int(lease_seconds) <= 3600:
            raise ValueError("任务租约必须位于 5 到 3600 秒之间。")
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        expires = (now + timedelta(seconds=lease_seconds)).isoformat()
        normalized_kinds = sorted({str(item).strip() for item in kinds or () if str(item).strip()})
        with self.repository.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            sql = (
                "SELECT id FROM jobs WHERE state='queued' AND cancel_requested=0 "
                "AND pause_requested=0 AND (resume_after='' OR resume_after<=?)"
            )
            params: list[object] = [now_text]
            if normalized_kinds:
                placeholders = ",".join("?" for _ in normalized_kinds)
                sql += f" AND kind IN ({placeholders})"
                params.extend(normalized_kinds)
            sql += " ORDER BY created_at,id LIMIT 1"
            row = db.execute(sql, params).fetchone()
            if row is None:
                return None
            identifier = str(row[0])
            cursor = db.execute(
                "UPDATE jobs SET state='running',lease_owner=?,lease_expires_at=?,"
                "heartbeat_at=?,attempt=attempt+1,message='',updated_at=? "
                "WHERE id=? AND state='queued'",
                (normalized_owner, expires, now_text, now_text, identifier),
            )
            if cursor.rowcount != 1:
                return None
            claimed = db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
        return self._job(claimed) if claimed else None

    def heartbeat(self, job_id: str, owner: str, *, lease_seconds: int = 30) -> bool:
        now = datetime.now(timezone.utc)
        with self.repository.connection() as db:
            cursor = db.execute(
                "UPDATE jobs SET heartbeat_at=?,lease_expires_at=?,updated_at=? "
                "WHERE id=? AND state='running' AND lease_owner=?",
                (
                    now.isoformat(),
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    now.isoformat(),
                    job_id,
                    owner,
                ),
            )
            return cursor.rowcount == 1

    def next_item(self, job_id: str, owner: str) -> JobItemRecord | None:
        now = utc_now()
        with self.repository.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute(
                "SELECT state,lease_owner,cancel_requested,pause_requested FROM jobs WHERE id=?",
                (job_id,),
            ).fetchone()
            if job is None or str(job[0]) != "running" or str(job[1]) != owner:
                return None
            if bool(job[2]) or bool(job[3]):
                return None
            row = db.execute(
                "SELECT * FROM job_items WHERE job_id=? AND state='pending' "
                "ORDER BY sequence LIMIT 1",
                (job_id,),
            ).fetchone()
            if row is None:
                return None
            item_id = int(row["id"])
            db.execute(
                "UPDATE job_items SET state='running',attempt=attempt+1,started_at=?,"
                "updated_at=? WHERE id=? AND state='pending'",
                (now, now, item_id),
            )
            claimed = db.execute("SELECT * FROM job_items WHERE id=?", (item_id,)).fetchone()
        return self._item(claimed) if claimed else None

    def item(self, item_id: int) -> JobItemRecord | None:
        with self.repository.connection() as db:
            row = db.execute("SELECT * FROM job_items WHERE id=?", (int(item_id),)).fetchone()
        return self._item(row) if row else None

    def finish_item(
        self,
        item_id: int,
        owner: str,
        *,
        output: Mapping[str, Any] | None = None,
    ) -> JobRecord:
        return self._complete_item(item_id, owner, "succeeded", output or {}, "", "")

    def fail_item(
        self,
        item_id: int,
        owner: str,
        *,
        error_code: str,
        message: str,
    ) -> JobRecord:
        return self._complete_item(item_id, owner, "failed", {}, error_code, message)

    def _complete_item(
        self,
        item_id: int,
        owner: str,
        state: str,
        output: Mapping[str, Any],
        error_code: str,
        message: str,
    ) -> JobRecord:
        if state not in TERMINAL_ITEM_STATES:
            raise ValueError("任务项终态无效。")
        instant = datetime.now(timezone.utc)
        now = instant.isoformat()
        expires = (instant + timedelta(seconds=30)).isoformat()
        with self.repository.connection() as db:
            row = db.execute(
                "SELECT job_id FROM job_items WHERE id=? AND state='running'", (item_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("任务项不存在或不在运行状态。")
            job_id = str(row[0])
            job = db.execute(
                "SELECT lease_owner FROM jobs WHERE id=? AND state='running'", (job_id,)
            ).fetchone()
            if job is None or str(job[0]) != owner:
                raise RuntimeError("批任务租约已经失效。")
            db.execute(
                "UPDATE job_items SET state=?,output_json=?,error_code=?,message=?,"
                "finished_at=?,updated_at=? WHERE id=?",
                (
                    state,
                    self._encode(output),
                    str(error_code)[:200],
                    str(message)[:2000],
                    now,
                    now,
                    item_id,
                ),
            )
            cursor = db.execute(
                "UPDATE jobs SET processed=processed+1,succeeded=succeeded+?,failed=failed+?,"
                "heartbeat_at=?,lease_expires_at=?,updated_at=? "
                "WHERE id=? AND state='running' AND lease_owner=?",
                (
                    1 if state == "succeeded" else 0,
                    1 if state == "failed" else 0,
                    now,
                    expires,
                    now,
                    job_id,
                    owner,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("批任务租约已经失效。")
            completed = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if completed is None:
            raise RuntimeError("批任务状态丢失。")
        return self._job(completed)

    def finish_job(
        self,
        job_id: str,
        owner: str,
        *,
        state: str = "succeeded",
        output: Mapping[str, Any] | None = None,
        error_code: str = "",
        message: str = "",
    ) -> JobRecord:
        if state not in TERMINAL_JOB_STATES:
            raise ValueError("批任务终态无效。")
        now = utc_now()
        with self.repository.connection() as db:
            cursor = db.execute(
                "UPDATE jobs SET state=?,output_json=?,error_code=?,message=?,"
                "lease_owner='',lease_expires_at='',heartbeat_at=?,updated_at=? "
                "WHERE id=? AND state='running' AND lease_owner=?",
                (
                    state,
                    self._encode(output or {}),
                    str(error_code)[:200],
                    str(message)[:2000],
                    now,
                    now,
                    job_id,
                    owner,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("批任务租约已经失效。")
            completed = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if completed is None:
            raise RuntimeError("批任务完成后无法读回。")
        return self._job(completed)

    def request_cancel(self, job_id: str) -> bool:
        now = utc_now()
        with self.repository.connection() as db:
            row = db.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or str(row[0]) in TERMINAL_JOB_STATES:
                return False
            state = str(row[0])
            if state in {"queued", "paused", "waiting_user"}:
                db.execute(
                    "UPDATE job_items SET state='cancelled',finished_at=?,updated_at=? "
                    "WHERE job_id=? AND state='pending'",
                    (now, now, job_id),
                )
                db.execute(
                    "UPDATE jobs SET state='cancelled',cancel_requested=1,message='用户取消',"
                    "updated_at=? WHERE id=?",
                    (now, job_id),
                )
            else:
                db.execute(
                    "UPDATE jobs SET cancel_requested=1,message='等待当前原子操作结束',"
                    "updated_at=? WHERE id=?",
                    (now, job_id),
                )
        return True

    def request_pause(self, job_id: str) -> bool:
        with self.repository.connection() as db:
            now = utc_now()
            cursor = db.execute(
                "UPDATE jobs SET state='paused',pause_requested=0,message='已暂停',updated_at=? "
                "WHERE id=? AND state='queued'",
                (now, job_id),
            )
            if cursor.rowcount == 0:
                cursor = db.execute(
                    "UPDATE jobs SET pause_requested=1,message='等待当前原子操作结束',"
                    "updated_at=? WHERE id=? AND state='running'",
                    (now, job_id),
                )
            return cursor.rowcount == 1

    def settle_control_request(self, job_id: str, owner: str) -> str:
        now = utc_now()
        with self.repository.connection() as db:
            row = db.execute(
                "SELECT cancel_requested,pause_requested FROM jobs "
                "WHERE id=? AND state='running' AND lease_owner=?",
                (job_id, owner),
            ).fetchone()
            if row is None:
                return "lost"
            if bool(row[0]):
                db.execute(
                    "UPDATE job_items SET state='cancelled',finished_at=?,updated_at=? "
                    "WHERE job_id=? AND state='pending'",
                    (now, now, job_id),
                )
                db.execute(
                    "UPDATE jobs SET state='cancelled',lease_owner='',lease_expires_at='',"
                    "message='用户取消',updated_at=? WHERE id=?",
                    (now, job_id),
                )
                return "cancelled"
            if bool(row[1]):
                db.execute(
                    "UPDATE jobs SET state='paused',pause_requested=0,lease_owner='',"
                    "lease_expires_at='',message='已暂停',updated_at=? WHERE id=?",
                    (now, job_id),
                )
                return "paused"
        return "running"

    def resume(self, job_id: str) -> bool:
        with self.repository.connection() as db:
            cursor = db.execute(
                "UPDATE jobs SET state='queued',pause_requested=0,cancel_requested=0,"
                "message='',updated_at=? WHERE id=? AND state IN ('paused','waiting_user')",
                (utc_now(), job_id),
            )
            return cursor.rowcount == 1

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

    @classmethod
    def _job(cls, row) -> JobRecord:
        return JobRecord(
            id=str(row["id"]),
            kind=str(row["kind"]),
            state=str(row["state"]),
            total=int(row["total"]),
            processed=int(row["processed"]),
            succeeded=int(row["succeeded"]),
            failed=int(row["failed"]),
            message=str(row["message"]),
            input=cls._decode(row["input_json"]),
            output=cls._decode(row["output_json"]),
            error_code=str(row["error_code"]),
            cancel_requested=bool(row["cancel_requested"]),
            pause_requested=bool(row["pause_requested"]),
            lease_owner=str(row["lease_owner"]),
            lease_expires_at=str(row["lease_expires_at"]),
            heartbeat_at=str(row["heartbeat_at"]),
            attempt=int(row["attempt"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @classmethod
    def _item(cls, row) -> JobItemRecord:
        return JobItemRecord(
            id=int(row["id"]),
            job_id=str(row["job_id"]),
            sequence=int(row["sequence"]),
            idempotency_key=str(row["idempotency_key"]),
            state=str(row["state"]),
            input=cls._decode(row["input_json"]),
            output=cls._decode(row["output_json"]),
            error_code=str(row["error_code"]),
            message=str(row["message"]),
            attempt=int(row["attempt"]),
            started_at=str(row["started_at"]),
            finished_at=str(row["finished_at"]),
        )


JobHandler = Callable[[JobRecord, JobItemRecord, threading.Event], Mapping[str, Any]]


class PersistentBatchRunner:
    def __init__(
        self,
        store: PersistentJobStore,
        handlers: Mapping[str, JobHandler],
        *,
        owner: str | None = None,
    ) -> None:
        self.store = store
        self.handlers = dict(handlers)
        self.owner = owner or "runner-" + uuid.uuid4().hex
        self.cancel = threading.Event()

    def run_once(self) -> JobRecord | None:
        self.store.recover_expired()
        job = self.store.claim(self.owner, kinds=self.handlers)
        if job is None:
            return None
        job_id = job.id
        handler = self.handlers[job.kind]
        continue_on_error = bool(job.input.get("continue_on_error"))
        keepalive_stop = threading.Event()

        def keepalive() -> None:
            while not keepalive_stop.wait(10):
                if not self.store.heartbeat(job_id, self.owner):
                    return

        keepalive_thread = threading.Thread(target=keepalive, daemon=True)
        keepalive_thread.start()
        try:
            while True:
                control = self.store.settle_control_request(job_id, self.owner)
                if control != "running":
                    return self.store.get(job_id)
                item = self.store.next_item(job_id, self.owner)
                if item is None:
                    current = self.store.get(job_id)
                    if current is None:
                        raise RuntimeError("批任务在执行期间消失。")
                    if current.processed >= current.total:
                        state = (
                            "failed" if current.failed and not continue_on_error else "succeeded"
                        )
                        return self.store.finish_job(job_id, self.owner, state=state)
                    return current
                try:
                    output = handler(job, item, self.cancel)
                    job = self.store.finish_item(item.id, self.owner, output=output)
                except InterruptedError as exc:
                    self.store.fail_item(
                        item.id, self.owner, error_code="cancelled", message=str(exc)
                    )
                    self.store.request_cancel(job_id)
                except Exception as exc:
                    job = self.store.fail_item(
                        item.id,
                        self.owner,
                        error_code=type(exc).__name__,
                        message=str(exc),
                    )
                    if not continue_on_error:
                        return self.store.finish_job(
                            job.id,
                            self.owner,
                            state="failed",
                            error_code=type(exc).__name__,
                            message=str(exc),
                        )
        finally:
            keepalive_stop.set()
            keepalive_thread.join(timeout=1)
