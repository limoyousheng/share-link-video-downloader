from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


JSON_COLUMNS = {"options_json", "metadata_json", "artifacts_json"}
UPDATABLE_COLUMNS = {
    "status",
    "phase",
    "progress",
    "message",
    "metadata_json",
    "artifacts_json",
    "error_code",
    "error_message",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class JobStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    owner_hash TEXT,
                    source_text TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    progress REAL NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    options_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    artifacts_json TEXT NOT NULL DEFAULT '[]',
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "owner_hash" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN owner_hash TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_jobs_owner_created "
                "ON jobs(owner_hash, created_at DESC)"
            )

    def mark_interrupted(self) -> None:
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'failed', phase = 'interrupted', progress = 0,
                    error_code = 'INTERRUPTED',
                    error_message = '程序上次运行时任务被中断，请重试。',
                    message = '任务被程序重启中断', updated_at = ?
                WHERE status IN ('queued', 'running')
                """,
                (now,),
            )

    def create(
        self,
        *,
        job_id: str,
        owner_hash: str,
        source_text: str,
        source_url: str,
        options: dict[str, Any],
    ) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, owner_hash, source_text, source_url, status, phase, progress, message,
                    options_json, metadata_json, artifacts_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', 'queued', 0, '等待处理', ?, '{}', '[]', ?, ?)
                """,
                (
                    job_id,
                    owner_hash,
                    source_text,
                    source_url,
                    json.dumps(options, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return self.get(job_id)

    def update(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        values: list[Any] = []
        assignments: list[str] = []
        for key, value in fields.items():
            if key not in UPDATABLE_COLUMNS:
                raise ValueError(f"Unsupported job field: {key}")
            if key in JSON_COLUMNS and not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False)
            assignments.append(f"{key} = ?")
            values.append(value)

        if not assignments:
            return self.get(job_id)

        assignments.append("updated_at = ?")
        values.append(utc_now())
        values.append(job_id)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE jobs SET {', '.join(assignments)} WHERE id = ?",
                values,
            )
        return self.get(job_id)

    def mark_cancelling(self, job_id: str) -> dict[str, Any] | None:
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'running', phase = 'cancelling', message = '正在取消任务…',
                    updated_at = ?
                WHERE id = ? AND status NOT IN ('completed', 'partial', 'failed', 'cancelled')
                """,
                (now, job_id),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._decode(row) if row else None

    def get_for_owner(self, job_id: str, owner_hash: str) -> dict[str, Any] | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ? AND owner_hash = ?",
                (job_id, owner_hash),
            ).fetchone()
        return self._decode(row) if row else None

    def has_active_for_owner(self, owner_hash: str) -> bool:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM jobs
                WHERE owner_hash = ? AND status IN ('queued', 'running')
                LIMIT 1
                """,
                (owner_hash,),
            ).fetchone()
        return row is not None

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 100))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (safe_limit,)
            ).fetchall()
        return [self._decode(row) for row in rows]

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        for column in JSON_COLUMNS:
            try:
                data[column.removesuffix("_json")] = json.loads(data.pop(column) or "null")
            except json.JSONDecodeError:
                data[column.removesuffix("_json")] = {} if column == "metadata_json" else []
        return data
