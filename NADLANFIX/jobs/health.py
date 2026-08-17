"""Health monitoring for NADLANFIX sources.

Provides:
- Per-source health tracking
- Health check endpoints
- Alerting on source failures
- Health history and trends
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class HealthStatus(Enum):
    """Health status levels."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    STALE = "stale"


@dataclass
class SourceHealth:
    """Health information for a single source."""
    source_name: str
    status: HealthStatus
    last_success_at: str | None = None
    last_failure_at: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    total_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    avg_duration_seconds: float = 0.0
    last_run_at: str | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        if self.total_runs == 0:
            return 0.0
        return self.successful_runs / self.total_runs

    @property
    def is_stale(self, max_age_hours: int = 24) -> bool:
        if self.last_success_at is None:
            return True
        last_success = datetime.fromisoformat(self.last_success_at)
        age = datetime.now(timezone.utc) - last_success
        return age.total_seconds() > max_age_hours * 3600

    def to_dict(self) -> dict:
        return {
            "source_name": self.source_name,
            "status": self.status.value,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "total_runs": self.total_runs,
            "successful_runs": self.successful_runs,
            "failed_runs": self.failed_runs,
            "success_rate": round(self.success_rate, 2),
            "avg_duration_seconds": round(self.avg_duration_seconds, 1),
            "last_run_at": self.last_run_at,
            "is_stale": self.is_stale(),
            "metadata": self.metadata,
        }


class HealthMonitor:
    """Monitor health of all data sources."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create health tables if they don't exist."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS source_health (
                    source_name TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'unknown',
                    last_success_at TEXT,
                    last_failure_at TEXT,
                    last_error TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    total_runs INTEGER NOT NULL DEFAULT 0,
                    successful_runs INTEGER NOT NULL DEFAULT 0,
                    failed_runs INTEGER NOT NULL DEFAULT 0,
                    avg_duration_seconds REAL NOT NULL DEFAULT 0,
                    last_run_at TEXT,
                    metadata TEXT,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS health_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_name TEXT NOT NULL,
                    alert_type TEXT NOT NULL,
                    message TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    resolved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    resolved_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_health_alerts_source
                    ON health_alerts (source_name, created_at);
            """)
            conn.commit()
        finally:
            conn.close()

    def record_success(self, source_name: str, duration_seconds: float = 0.0, metadata: dict | None = None) -> None:
        """Record a successful run for a source."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            row = conn.execute(
                "SELECT total_runs, successful_runs FROM source_health WHERE source_name = ?",
                (source_name,)
            ).fetchone()
            total_runs = (row[0] if row else 0) + 1
            successful_runs = (row[1] if row else 0) + 1
            conn.execute(
                """INSERT OR REPLACE INTO source_health
                   (source_name, status, last_success_at, consecutive_failures,
                    total_runs, successful_runs, last_run_at, metadata, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source_name,
                    HealthStatus.HEALTHY.value,
                    now,
                    0,
                    total_runs,
                    successful_runs,
                    now,
                    json.dumps(metadata or {}),
                    now,
                )
            )
            conn.commit()
        finally:
            conn.close()

    def record_failure(self, source_name: str, error: str, metadata: dict | None = None) -> None:
        """Record a failed run for a source."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            row = conn.execute(
                "SELECT consecutive_failures, total_runs, failed_runs FROM source_health WHERE source_name = ?",
                (source_name,)
            ).fetchone()
            consecutive = (row[0] if row else 0) + 1
            total_runs = (row[1] if row else 0) + 1
            failed_runs = (row[2] if row else 0) + 1
            conn.execute(
                """INSERT OR REPLACE INTO source_health
                   (source_name, status, last_failure_at, last_error,
                    consecutive_failures, total_runs, failed_runs, last_run_at,
                    metadata, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source_name,
                    HealthStatus.FAILED.value,
                    now,
                    error,
                    consecutive,
                    total_runs,
                    failed_runs,
                    now,
                    json.dumps(metadata or {}),
                    now,
                )
            )
            conn.commit()
        finally:
            conn.close()

    def get_health(self, source_name: str) -> SourceHealth:
        """Get health status for a specific source."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM source_health WHERE source_name = ?",
                (source_name,)
            ).fetchone()
            if row:
                return SourceHealth(
                    source_name=row["source_name"],
                    status=HealthStatus(row["status"]),
                    last_success_at=row["last_success_at"],
                    last_failure_at=row["last_failure_at"],
                    last_error=row["last_error"],
                    consecutive_failures=row["consecutive_failures"],
                    total_runs=row["total_runs"],
                    successful_runs=row["successful_runs"],
                    failed_runs=row["failed_runs"],
                    avg_duration_seconds=row["avg_duration_seconds"],
                    last_run_at=row["last_run_at"],
                    metadata=json.loads(row["metadata"] or "{}"),
                )
            return SourceHealth(source_name=source_name, status=HealthStatus.UNKNOWN)
        finally:
            conn.close()

    def get_all_health(self) -> list[SourceHealth]:
        """Get health status for all sources."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM source_health ORDER BY updated_at DESC").fetchall()
            return [
                SourceHealth(
                    source_name=row["source_name"],
                    status=HealthStatus(row["status"]),
                    last_success_at=row["last_success_at"],
                    last_failure_at=row["last_failure_at"],
                    last_error=row["last_error"],
                    consecutive_failures=row["consecutive_failures"],
                    total_runs=row["total_runs"],
                    successful_runs=row["successful_runs"],
                    failed_runs=row["failed_runs"],
                    avg_duration_seconds=row["avg_duration_seconds"],
                    last_run_at=row["last_run_at"],
                    metadata=json.loads(row["metadata"] or "{}"),
                )
                for row in rows
            ]
        finally:
            conn.close()

    def get_unhealthy_sources(self, max_consecutive_failures: int = 3) -> list[SourceHealth]:
        """Get sources that are considered unhealthy."""
        all_health = self.get_all_health()
        return [
            h for h in all_health
            if h.consecutive_failures >= max_consecutive_failures
            or h.status in (HealthStatus.FAILED, HealthStatus.STALE)
        ]

    def create_alert(self, source_name: str, alert_type: str, message: str, severity: str = "warning") -> int:
        """Create a health alert."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.execute(
                """INSERT INTO health_alerts
                   (source_name, alert_type, message, severity)
                   VALUES (?, ?, ?, ?)""",
                (source_name, alert_type, message, severity)
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def get_active_alerts(self) -> list[dict]:
        """Get all unresolved alerts."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM health_alerts WHERE resolved = 0 ORDER BY created_at DESC"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def resolve_alert(self, alert_id: int) -> None:
        """Mark an alert as resolved."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            conn.execute(
                "UPDATE health_alerts SET resolved = 1, resolved_at = ? WHERE id = ?",
                (now, alert_id)
            )
            conn.commit()
        finally:
            conn.close()
