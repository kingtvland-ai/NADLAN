"""Observability for NADLANFIX.

Provides:
- Request metrics
- Job metrics
- Data quality indicators
- Failure alerting
- Structured logging
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


@dataclass
class Metric:
    """A single metric observation."""
    name: str
    value: float
    tags: dict[str, str] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


@dataclass
class Alert:
    """System alert."""
    alert_id: str
    severity: str  # 'info', 'warning', 'error', 'critical'
    message: str
    source: str
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    resolved_at: str | None = None
    is_resolved: bool = False


class Observability:
    """Observability manager for NADLANFIX."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create observability tables if they don't exist."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    value REAL NOT NULL,
                    tags TEXT,
                    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS alerts (
                    alert_id TEXT PRIMARY KEY,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    source TEXT NOT NULL,
                    metadata TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    resolved_at TEXT,
                    is_resolved INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS request_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    duration_ms REAL NOT NULL,
                    user_id INTEGER,
                    ip_address TEXT,
                    user_agent TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS job_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_name TEXT NOT NULL,
                    source TEXT,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    duration_seconds REAL,
                    rows_processed INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    metadata TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE INDEX IF NOT EXISTS idx_metrics_name ON metrics (name, timestamp);
                CREATE INDEX IF NOT EXISTS idx_alerts_source ON alerts (source, is_resolved, created_at);
                CREATE INDEX IF NOT EXISTS idx_request_log_path ON request_log (path, created_at);
                CREATE INDEX IF NOT EXISTS idx_job_log_name ON job_log (job_name, started_at);
            """)
            conn.commit()
        finally:
            conn.close()

    def record_metric(self, metric: Metric) -> None:
        """Record a metric."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "INSERT INTO metrics (name, value, tags, timestamp) VALUES (?, ?, ?, ?)",
                (metric.name, metric.value, json.dumps(metric.tags), metric.timestamp)
            )
            conn.commit()
        finally:
            conn.close()

    def record_request(self, method: str, path: str, status_code: int,
                       duration_ms: float, user_id: int | None = None,
                       ip_address: str | None = None, user_agent: str | None = None) -> None:
        """Record an HTTP request."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                """INSERT INTO request_log
                   (method, path, status_code, duration_ms, user_id, ip_address, user_agent)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (method, path, status_code, duration_ms, user_id, ip_address, user_agent)
            )
            conn.commit()
        finally:
            conn.close()

    def record_job(self, job_name: str, status: str, started_at: str,
                   finished_at: str | None = None, duration_seconds: float = 0.0,
                   rows_processed: int = 0, error: str | None = None,
                   source: str | None = None, metadata: dict | None = None) -> None:
        """Record a job execution."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                """INSERT INTO job_log
                   (job_name, source, status, started_at, finished_at,
                    duration_seconds, rows_processed, error, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_name, source, status, started_at, finished_at,
                 duration_seconds, rows_processed, error, json.dumps(metadata or {}))
            )
            conn.commit()
        finally:
            conn.close()

    def create_alert(self, alert: Alert) -> None:
        """Create an alert."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "INSERT OR REPLACE INTO alerts (alert_id, severity, message, source, metadata, created_at, resolved_at, is_resolved) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (alert.alert_id, alert.severity, alert.message, alert.source,
                 json.dumps(alert.metadata), alert.created_at, alert.resolved_at,
                 1 if alert.is_resolved else 0)
            )
            conn.commit()
        finally:
            conn.close()

    def resolve_alert(self, alert_id: str) -> None:
        """Resolve an alert."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            conn.execute(
                "UPDATE alerts SET is_resolved = 1, resolved_at = ? WHERE alert_id = ?",
                (now, alert_id)
            )
            conn.commit()
        finally:
            conn.close()

    def get_active_alerts(self, severity: str | None = None) -> list[dict]:
        """Get active alerts."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            query = "SELECT * FROM alerts WHERE is_resolved = 0"
            params = []
            if severity:
                query += " AND severity = ?"
                params.append(severity)
            query += " ORDER BY created_at DESC"

            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_metrics(self, name: str, start_time: str | None = None,
                    end_time: str | None = None, limit: int = 1000) -> list[dict]:
        """Get metrics for a specific name."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            query = "SELECT * FROM metrics WHERE name = ?"
            params = [name]

            if start_time:
                query += " AND timestamp >= ?"
                params.append(start_time)
            if end_time:
                query += " AND timestamp <= ?"
                params.append(end_time)

            query += " ORDER BY timestamp DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_request_stats(self, path: str | None = None, limit: int = 100) -> dict:
        """Get request statistics."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            stats = {}
            if path:
                rows = conn.execute(
                    "SELECT status_code, COUNT(*) as count, AVG(duration_ms) as avg_duration "
                    "FROM request_log WHERE path = ? GROUP BY status_code",
                    (path,)
                ).fetchall()
                stats["by_status"] = {row[0]: {"count": row[1], "avg_duration_ms": row[2]} for row in rows}
                stats["total"] = sum(row["count"] for row in stats["by_status"].values())
            else:
                stats["total"] = conn.execute("SELECT COUNT(*) FROM request_log").fetchone()[0]
                stats["recent"] = conn.execute(
                    "SELECT method, path, status_code, duration_ms, created_at "
                    "FROM request_log ORDER BY created_at DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            return stats
        finally:
            conn.close()

    def get_job_stats(self, job_name: str | None = None, limit: int = 50) -> list[dict]:
        """Get job execution statistics."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            query = "SELECT * FROM job_log"
            params = []
            if job_name:
                query += " WHERE job_name = ?"
                params.append(job_name)
            query += " ORDER BY started_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    # -- Dashboard metrics --

    def get_dashboard_metrics(self) -> dict:
        """Get aggregated metrics for the dashboard."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            total_requests = conn.execute("SELECT COUNT(*) FROM request_log").fetchone()[0]
            error_requests = conn.execute("SELECT COUNT(*) FROM request_log WHERE status_code >= 400").fetchone()[0]
            avg_duration = conn.execute("SELECT AVG(duration_ms) FROM request_log").fetchone()[0] or 0.0

            total_jobs = conn.execute("SELECT COUNT(*) FROM job_log").fetchone()[0]
            failed_jobs = conn.execute("SELECT COUNT(*) FROM job_log WHERE status = 'failed'").fetchone()[0]

            active_alerts = conn.execute("SELECT COUNT(*) FROM alerts WHERE is_resolved = 0").fetchone()[0]

            return {
                "requests_total": total_requests,
                "requests_error": error_requests,
                "requests_error_rate": (error_requests / total_requests * 100) if total_requests else 0.0,
                "requests_avg_duration_ms": round(avg_duration, 2),
                "jobs_total": total_jobs,
                "jobs_failed": failed_jobs,
                "jobs_success_rate": ((total_jobs - failed_jobs) / total_jobs * 100) if total_jobs else 0.0,
                "alerts_active": active_alerts,
            }
        finally:
            conn.close()

    def get_source_metrics(self, source: str, limit: int = 50) -> dict:
        """Get metrics for a specific source."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            jobs = conn.execute(
                "SELECT * FROM job_log WHERE source = ? ORDER BY started_at DESC LIMIT ?",
                (source, limit)
            ).fetchall()
            metrics = conn.execute(
                "SELECT * FROM metrics WHERE tags LIKE ? ORDER BY timestamp DESC LIMIT ?",
                (f'%"{source}"%', limit)
            ).fetchall()
            return {
                "jobs": [dict(row) for row in jobs],
                "metrics": [dict(row) for row in metrics],
            }
        finally:
            conn.close()

    # -- Freshness monitor --

    def check_freshness(self, source_name: str, last_success_at: str | None, threshold_hours: float = 24.0) -> dict:
        """Check data freshness for a source.

        Args:
            source_name: Source identifier
            last_success_at: ISO timestamp of last successful run
            threshold_hours: Hours before data is considered stale

        Returns:
            Freshness status dict
        """
        if not last_success_at:
            return {
                "source_name": source_name,
                "status": "stale",
                "last_success_at": None,
                "hours_since": None,
                "threshold_hours": threshold_hours,
            }

        last_success_dt = datetime.fromisoformat(last_success_at)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        delta = now - last_success_dt
        hours_since = delta.total_seconds() / 3600

        return {
            "source_name": source_name,
            "status": "stale" if hours_since > threshold_hours else "fresh",
            "last_success_at": last_success_at,
            "hours_since": round(hours_since, 2),
            "threshold_hours": threshold_hours,
        }

    def get_freshness_status(self, sources: list[dict], threshold_hours: float = 24.0) -> list[dict]:
        """Get freshness status for multiple sources.

        Args:
            sources: List of source health dicts with source_name and last_success_at
            threshold_hours: Hours before data is considered stale

        Returns:
            List of freshness status dicts
        """
        return [
            self.check_freshness(
                source_name=s.get("source_name", ""),
                last_success_at=s.get("last_success_at"),
                threshold_hours=threshold_hours,
            )
            for s in sources
        ]
