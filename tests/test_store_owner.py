from __future__ import annotations

import sqlite3
from pathlib import Path

from app.store import JobStore


def create_job(store: JobStore, job_id: str, owner_hash: str) -> dict:
    return store.create(
        job_id=job_id,
        owner_hash=owner_hash,
        source_text="https://v.douyin.com/example/",
        source_url="https://v.douyin.com/example/",
        options={"download_video": True},
    )


def test_job_store_isolates_jobs_and_active_state_by_owner(tmp_path: Path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    owner_a = "a" * 64
    owner_b = "b" * 64
    job_a = create_job(store, "job-a", owner_a)
    job_b = create_job(store, "job-b", owner_b)

    assert job_a["owner_hash"] == owner_a
    assert job_b["owner_hash"] == owner_b
    assert store.get_for_owner("job-a", owner_a)["id"] == "job-a"
    assert store.get_for_owner("job-a", owner_b) is None
    assert store.get_for_owner("job-b", owner_a) is None
    assert store.get_for_owner("job-b", owner_b)["id"] == "job-b"
    assert store.has_active_for_owner(owner_a) is True
    assert store.has_active_for_owner(owner_b) is True

    store.update("job-a", status="completed", phase="completed", progress=100)
    assert store.has_active_for_owner(owner_a) is False
    assert store.has_active_for_owner(owner_b) is True


def test_job_store_migrates_legacy_database_without_exposing_old_jobs(tmp_path: Path):
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
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
        connection.execute(
            """
            INSERT INTO jobs (
                id, source_text, source_url, status, phase, progress, message,
                options_json, metadata_json, artifacts_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'completed', 'completed', 100, '处理完成', '{}', '{}', '[]', ?, ?)
            """,
            (
                "legacy-job",
                "https://v.douyin.com/legacy/",
                "https://v.douyin.com/legacy/",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    store = JobStore(database)
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(jobs)")}

    assert "owner_hash" in columns
    assert "idx_jobs_owner_created" in indexes
    assert store.get("legacy-job")["owner_hash"] is None
    assert store.get_for_owner("legacy-job", "") is None
    assert store.get_for_owner("legacy-job", "a" * 64) is None

    created = create_job(store, "new-job", "a" * 64)
    assert created["owner_hash"] == "a" * 64
    assert store.get_for_owner("new-job", "a" * 64)["id"] == "new-job"
