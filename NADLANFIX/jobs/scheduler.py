"""Unified scheduler for NADLANFIX source ingestion.

Provides:
- Daily orchestration of all sources
- Per-source run locks to prevent concurrent execution
- Retry logic with exponential backoff
- Health tracking and status API
- Run history and audit trail
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from sources.base.adapter import BaseSourceAdapter, SourceRunState, SourceHealthInfo, SourceStatus

try:
    from storage.firestore_sync import FirestoreSync
    HAS_FIRESTORE = True
except ImportError:
    HAS_FIRESTORE = False


class JobStatus(Enum):
    """Status of a scheduled job."""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRYING = "retrying"


@dataclass
class JobResult:
    """Result of a single job execution."""
    job_name: str
    status: JobStatus
    started_at: str
    finished_at: Optional[str] = None
    duration_seconds: float = 0.0
    rows_processed: int = 0
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "job_name": self.job_name,
            "status": self.status.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "rows_processed": self.rows_processed,
            "error": self.error,
            "metadata": self.metadata,
        }


@dataclass
class SchedulerConfig:
    """Configuration for the scheduler."""
    max_retries: int = 3
    retry_delay_seconds: float = 60.0
    retry_backoff_multiplier: float = 2.0
    job_timeout_seconds: float = 3600.0  # 1 hour
    health_check_interval_seconds: float = 300.0  # 5 minutes
    stale_threshold_hours: float = 24.0


class UnifiedScheduler:
    """Unified scheduler for all data sources.

    Features:
    - Daily orchestration with configurable schedules
    - Per-source run locks (prevents concurrent execution)
    - Retry with exponential backoff
    - Health tracking per source
    - Run history and audit trail
    - Status API for monitoring
    """

    def __init__(self, db_path: Path | str, config: SchedulerConfig | None = None):
        self.db_path = Path(db_path)
        self.config = config or SchedulerConfig()
        self._lock = threading.Lock()
        self._running_jobs: dict[str, threading.Thread] = {}
        self._job_results: list[JobResult] = []

        self._ensure_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Create scheduler tables if they don't exist."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            has_runs = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduler_runs'"
            ).fetchone()
            if not has_runs:
                conn.execute("""
                    CREATE TABLE scheduler_runs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_name TEXT NOT NULL,
                        source_name TEXT,
                        status TEXT NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
                        duration_seconds REAL,
                        rows_processed INTEGER NOT NULL DEFAULT 0,
                        error TEXT,
                        metadata TEXT,
                        created_at TEXT NOT NULL DEFAULT (datetime('now'))
                    )
                """)
            columns = [r[1] for r in conn.execute("PRAGMA table_info('scheduler_runs')")]
            if 'job_name' not in columns:
                self._migrate_legacy_scheduler_schema(conn)

            has_health = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduler_health'"
            ).fetchone()
            if not has_health:
                conn.execute("""
                    CREATE TABLE scheduler_health (
                        source_name TEXT PRIMARY KEY,
                        health TEXT NOT NULL DEFAULT 'unknown',
                        last_success_at TEXT,
                        last_failure_at TEXT,
                        last_error TEXT,
                        consecutive_failures INTEGER NOT NULL DEFAULT 0,
                        total_runs INTEGER NOT NULL DEFAULT 0,
                        successful_runs INTEGER NOT NULL DEFAULT 0,
                        metadata TEXT,
                        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                    )
                """)

            conn.execute("CREATE INDEX IF NOT EXISTS idx_scheduler_runs_job ON scheduler_runs (job_name, started_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_scheduler_runs_source ON scheduler_runs (source_name, started_at)")
            conn.commit()
        finally:
            conn.close()

    def _migrate_legacy_scheduler_schema(self, conn: sqlite3.Connection) -> None:
        """Upgrade the legacy scheduler_runs schema used in older code."""
        legacy_columns = [r[1] for r in conn.execute("PRAGMA table_info('scheduler_runs')")]
        if not legacy_columns:
            return

        if 'job_name' in legacy_columns:
            return

        if 'job' in legacy_columns:
            conn.execute("ALTER TABLE scheduler_runs RENAME TO scheduler_runs_legacy")
            conn.execute("""
                CREATE TABLE scheduler_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_name TEXT NOT NULL,
                    source_name TEXT,
                    status TEXT NOT NULL DEFAULT 'success',
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    duration_seconds REAL,
                    rows_processed INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    metadata TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
            """)
            conn.execute("""
                INSERT INTO scheduler_runs (
                    id, job_name, source_name, status, started_at, finished_at,
                    duration_seconds, rows_processed, error, metadata, created_at
                )
                SELECT
                    id,
                    job,
                    NULL,
                    CASE WHEN ok = 1 AND skipped = 0 THEN 'success' ELSE 'failed' END,
                    started_at,
                    finished_at,
                    0,
                    0,
                    error,
                    CASE WHEN detail IS NOT NULL THEN json_object('legacy_detail', detail) ELSE NULL END,
                    datetime('now')
                FROM scheduler_runs_legacy
            """)
            conn.execute("DROP TABLE scheduler_runs_legacy")
            conn.commit()

            column_names = [r[1] for r in conn.execute("PRAGMA table_info('scheduler_runs')")]
            if 'job_name' not in column_names:
                raise sqlite3.OperationalError("scheduler_runs migration failed to create job_name column")

    # ------------------------------------------------------------------
    # Job execution
    # ------------------------------------------------------------------

    def run_job(self, job_name: str, source: BaseSourceAdapter, target: int = 500, **kwargs) -> JobResult:
        """Run a single source job with retry logic.

        Args:
            job_name: Unique job identifier
            source: Source adapter instance
            target: Target number of listings
            **kwargs: Additional parameters for the source

        Returns:
            JobResult with execution details
        """
        started_at = self._now()
        result = JobResult(job_name=job_name, status=JobStatus.RUNNING, started_at=started_at)

        # Acquire file-based lock
        if not self._acquire_lock(job_name):
            result.status = JobStatus.SKIPPED
            result.error = "Job already locked by another process"
            result.finished_at = self._now()
            self._job_results.append(result)
            return result

        try:
            # Check if already running in memory
            with self._lock:
                if job_name in self._running_jobs and self._running_jobs[job_name].is_alive():
                    result.status = JobStatus.SKIPPED
                    result.error = "Job already running"
                    result.finished_at = self._now()
                    self._job_results.append(result)
                    return result

            # Run with retry
            attempt = 0
            last_error = None

            while attempt <= self.config.max_retries:
                if attempt > 0:
                    result.status = JobStatus.RETRYING
                    time.sleep(self.config.retry_delay_seconds * (self.config.retry_backoff_multiplier ** (attempt - 1)))

                try:
                    conn = source.get_conn()
                    try:
                        run_state = source.run(conn, target=target, **kwargs)
                        result.rows_processed = run_state.rows_new + run_state.rows_updated
                        result.status = JobStatus.SUCCESS
                        result.metadata = run_state.to_dict()
                        last_error = None
                        break
                    finally:
                        conn.close()
                except Exception as exc:
                    last_error = str(exc)
                    attempt += 1

            if result.status == JobStatus.SUCCESS and HAS_FIRESTORE:
                try:
                    sync = FirestoreSync(self.db_path)
                    sync_result = sync.sync_listings()
                    result.metadata["firestore_sync"] = sync_result
                except Exception as exc:
                    result.metadata["firestore_sync_error"] = str(exc)

            result.finished_at = self._now()
            result.duration_seconds = (
                datetime.fromisoformat(result.finished_at) - datetime.fromisoformat(started_at)
            ).total_seconds()

            if last_error:
                result.status = JobStatus.FAILED
                result.error = last_error
                self._alert_on_failure(result)
        finally:
            self._release_lock(job_name)

        self._job_results.append(result)
        self._persist_result(result)

        return result

    def run_daily_cycle(self, sources: dict[str, BaseSourceAdapter], targets: dict[str, int] | None = None) -> list[JobResult]:
        """Run all sources in a daily cycle.

        Args:
            sources: Dict of job_name -> source adapter
            targets: Dict of job_name -> target count (optional)

        Returns:
            List of JobResult for each source
        """
        results = []
        targets = targets or {}

        stale = self.check_stale_sources()
        if stale:
            try:
                from legacy.tools import alerts
                alerts.raise_alert(
                    title="NADLANFIX stale sources detected",
                    message=f"{len(stale)} source(s) have not run successfully within the stale threshold.",
                    severity="warning",
                    source="scheduler",
                    metadata={"stale_sources": stale},
                )
            except Exception:
                pass

        for job_name, source in sources.items():
            target = targets.get(job_name, 500)
            result = self.run_job(job_name, source, target=target)
            results.append(result)

        return results

    # ------------------------------------------------------------------
    # Health and status
    # ------------------------------------------------------------------

    def get_health(self, source_name: str) -> dict:
        """Get health status for a specific source."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM scheduler_health WHERE source_name = ?",
                (source_name,)
            ).fetchone()
            if row:
                return dict(row)
            return {"source_name": source_name, "health": "unknown"}
        finally:
            conn.close()

    def get_all_health(self) -> list[dict]:
        """Get health status for all sources."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM scheduler_health ORDER BY updated_at DESC").fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_recent_runs(self, limit: int = 50) -> list[dict]:
        """Get recent job runs."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM scheduler_runs ORDER BY started_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_source_runs(self, source_name: str, limit: int = 20) -> list[dict]:
        """Get recent runs for a specific source."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM scheduler_runs WHERE source_name = ? ORDER BY started_at DESC LIMIT ?",
                (source_name, limit)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist_result(self, result: JobResult) -> None:
        """Save job result to database."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                """INSERT INTO scheduler_runs
                   (job_name, source_name, status, started_at, finished_at,
                    duration_seconds, rows_processed, error, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    result.job_name,
                    result.metadata.get("source_name"),
                    result.status.value,
                    result.started_at,
                    result.finished_at,
                    result.duration_seconds,
                    result.rows_processed,
                    result.error,
                    json.dumps(result.metadata),
                )
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ------------------------------------------------------------------
    # File-based locking
    # ------------------------------------------------------------------

    def _lock_path(self, job_name: str) -> Path:
        safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", job_name)
        return self.db_path.parent / f".scheduler_lock_{safe_name}.lock"

    def _acquire_lock(self, job_name: str, timeout_seconds: float = 5.0) -> bool:
        """Acquire a file-based lock for a job.

        Args:
            job_name: Job identifier
            timeout_seconds: Max time to wait for lock

        Returns:
            True if lock acquired, False otherwise
        """
        lock_path = self._lock_path(job_name)
        start = time.time()
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return True
            except FileExistsError:
                if time.time() - start > timeout_seconds:
                    return False
                time.sleep(0.25)

    def _release_lock(self, job_name: str) -> None:
        """Release a file-based lock for a job."""
        lock_path = self._lock_path(job_name)
        try:
            lock_path.unlink(missing_ok=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Stale data detection
    # ------------------------------------------------------------------

    def is_stale(self, source_name: str) -> bool:
        """Check if a source's data is stale.

        Args:
            source_name: Source identifier

        Returns:
            True if data is stale, False otherwise
        """
        health = self.get_health(source_name)
        last_success = health.get("last_success_at")
        if not last_success:
            return True

        last_success_dt = datetime.fromisoformat(last_success)
        threshold = datetime.now(timezone.utc) - timedelta(hours=self.config.stale_threshold_hours)
        return last_success_dt < threshold.replace(tzinfo=None)

    def check_stale_sources(self) -> list[dict]:
        """Check all sources for stale data.

        Returns:
            List of stale sources with details
        """
        stale = []
        for health in self.get_all_health():
            if self.is_stale(health["source_name"]):
                stale.append({
                    "source_name": health["source_name"],
                    "last_success_at": health.get("last_success_at"),
                    "consecutive_failures": health.get("consecutive_failures", 0),
                    "last_error": health.get("last_error"),
                })
        return stale

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    def _alert_on_failure(self, result: JobResult) -> None:
        """Alert on job failure.

        Args:
            result: Job result with failure details
        """
        try:
            from legacy.tools import alerts
            alerts.raise_alert(
                title=f"NADLANFIX job failed: {result.job_name}",
                message=result.error or "Unknown error",
                severity="error",
                source="scheduler",
                metadata=result.to_dict(),
            )
        except Exception:
            pass
