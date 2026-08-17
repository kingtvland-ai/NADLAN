"""Unified source adapter interface for NADLANFIX.

All data sources (Yad2, Facebook, ONMAP, etc.) must implement this interface.
This ensures consistent behavior, testing, and lifecycle management across
all sources.

Architecture:
    sources/
    ├── base/
    │   ├── adapter.py      # BaseSourceAdapter interface
    │   ├── models.py       # Canonical listing models
    │   └── exceptions.py   # Source-specific exceptions
    ├── yad2/
    │   └── adapter.py      # Yad2 implementation
    ├── facebook/
    │   └── adapter.py      # Facebook implementation
    ├── onmap/
    │   └── adapter.py      # ONMAP implementation
    └── shared/
        └── utils.py        # Shared utilities
"""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class SourceStatus(Enum):
    """Operational status of a source."""
    IDLE = "idle"
    RUNNING = "running"
    COMPLETE = "complete"
    EXHAUSTED = "exhausted"
    FAILED = "failed"
    STOPPED = "stopped"
    STALE = "stale"


class SourceHealth(Enum):
    """Health status of a source."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass
class SourceRunState:
    """State of a single source run."""
    source_name: str
    status: SourceStatus
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    pages_fetched: int = 0
    pages_failed: int = 0
    rows_seen: int = 0
    rows_new: int = 0
    rows_updated: int = 0
    target: Optional[int] = None
    error: Optional[str] = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "source_name": self.source_name,
            "status": self.status.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "pages_fetched": self.pages_fetched,
            "pages_failed": self.pages_failed,
            "rows_seen": self.rows_seen,
            "rows_new": self.rows_new,
            "rows_updated": self.rows_updated,
            "target": self.target,
            "error": self.error,
            "metadata": self.metadata,
        }


@dataclass
class SourceHealthInfo:
    """Health information for a source."""
    source_name: str
    health: SourceHealth
    last_success_at: Optional[str] = None
    last_failure_at: Optional[str] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    total_runs: int = 0
    successful_runs: int = 0
    metadata: dict = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        if self.total_runs == 0:
            return 0.0
        return self.successful_runs / self.total_runs

    def to_dict(self) -> dict:
        return {
            "source_name": self.source_name,
            "health": self.health.value,
            "last_success_at": self.last_success_at,
            "last_failure_at": self.last_failure_at,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "total_runs": self.total_runs,
            "successful_runs": self.successful_runs,
            "success_rate": round(self.success_rate, 2),
            "metadata": self.metadata,
        }


class SourceAdapterError(Exception):
    """Base exception for source adapter errors."""
    pass


class SourceFetchError(SourceAdapterError):
    """Error during data fetching."""
    pass


class SourceNormalizeError(SourceAdapterError):
    """Error during data normalization."""
    pass


class SourcePersistError(SourceAdapterError):
    """Error during data persistence."""
    pass


class BaseSourceAdapter(ABC):
    """Abstract base class for all data source adapters.

    Each source (Yad2, Facebook, ONMAP) must implement this interface
    to ensure consistent behavior across the system.
    """

    def __init__(self, db_path: Path | str, *, config: dict | None = None):
        self.db_path = Path(db_path)
        self.config = config or {}
        self._state: SourceRunState | None = None
        self._health: SourceHealthInfo = self._load_health()

    # ------------------------------------------------------------------
    # Abstract methods - must be implemented by each source
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Unique identifier for this source (e.g., 'yad2', 'facebook', 'onmap')."""
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name (e.g., 'יד 2', 'Facebook Marketplace')."""
        pass

    @abstractmethod
    def ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Create tables and indexes if they don't exist."""
        pass

    @abstractmethod
    def fetch(self, conn: sqlite3.Connection, target: int, **kwargs) -> list[dict]:
        """Fetch raw listings from the source.

        Args:
            conn: Database connection
            target: Target number of listings to fetch
            **kwargs: Source-specific parameters

        Returns:
            List of raw listing dicts
        """
        pass

    @abstractmethod
    def normalize(self, raw: dict) -> dict:
        """Convert raw listing to canonical format.

        Args:
            raw: Raw listing from fetch()

        Returns:
            Canonical listing dict
        """
        pass

    @abstractmethod
    def count(self, conn: sqlite3.Connection) -> dict:
        """Return count and freshness info for this source.

        Returns:
            Dict with keys: total, last_seen, new_today, etc.
        """
        pass

    # ------------------------------------------------------------------
    # Concrete methods - shared implementation
    # ------------------------------------------------------------------

    def get_conn(self) -> sqlite3.Connection:
        """Get a database connection with WAL mode and foreign keys."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def state(self) -> dict:
        """Return current run state."""
        if self._state is None:
            return {"status": SourceStatus.IDLE.value}
        return self._state.to_dict()

    def health(self) -> dict:
        """Return health information."""
        return self._health.to_dict()

    def is_running(self) -> bool:
        """Check if a run is currently in progress."""
        return self._state is not None and self._state.status == SourceStatus.RUNNING

    def is_stale(self, max_age_hours: int = 24) -> bool:
        """Check if the source data is stale."""
        if self._health.last_success_at is None:
            return True
        last_success = datetime.fromisoformat(self._health.last_success_at)
        age = datetime.now(timezone.utc) - last_success
        return age.total_seconds() > max_age_hours * 3600

    def run(self, conn: sqlite3.Connection, target: int = 500, **kwargs) -> SourceRunState:
        """Execute a full fetch-normalize-validate-persist cycle.

        This is the main entry point for running a source job.
        It handles state management, error handling, and health tracking.

        Args:
            conn: Database connection
            target: Target number of listings
            **kwargs: Additional parameters passed to fetch()

        Returns:
            Final run state
        """
        from jobs.validation import ListingValidator
        validator = ListingValidator()

        self.ensure_schema(conn)

        self._state = SourceRunState(
            source_name=self.source_name,
            status=SourceStatus.RUNNING,
            started_at=self._now(),
            target=target,
        )

        try:
            raw_listings = self.fetch(conn, target, **kwargs)
            self._state.rows_seen = len(raw_listings)

            inserted = 0
            updated = 0
            validation_errors = 0

            for raw in raw_listings:
                try:
                    canonical = self.normalize(raw)

                    validation_result = validator.validate(canonical)
                    if not validation_result.is_valid:
                        validation_errors += 1
                        continue

                    if self._persist_listing(conn, validation_result.normalized or canonical):
                        inserted += 1
                    else:
                        updated += 1
                except Exception:
                    continue

            conn.commit()

            self._state.rows_new = inserted
            self._state.rows_updated = updated
            self._state.status = SourceStatus.COMPLETE if inserted > 0 else SourceStatus.EXHAUSTED
            self._state.finished_at = self._now()
            self._state.metadata["validation_errors"] = validation_errors

            self._record_success()

        except Exception as exc:
            self._state.status = SourceStatus.FAILED
            self._state.error = str(exc)
            self._state.finished_at = self._now()
            self._record_failure(str(exc))

        state = self._state
        self._state = None
        return state

    def _persist_listing(self, conn: sqlite3.Connection, listing: dict) -> bool:
        """Persist a canonical listing. Returns True if inserted, False if updated."""
        raise NotImplementedError("Each source must implement _persist_listing")

    def _load_health(self) -> SourceHealthInfo:
        """Load health info from database."""
        try:
            conn = self.get_conn()
            try:
                row = conn.execute(
                    "SELECT * FROM source_health WHERE source_name = ?",
                    (self.source_name,)
                ).fetchone()
                if row:
                    return SourceHealthInfo(
                        source_name=row["source_name"],
                        health=SourceHealth(row["status"]),
                        last_success_at=row["last_success_at"],
                        last_failure_at=row["last_failure_at"],
                        last_error=row["last_error"],
                        consecutive_failures=row["consecutive_failures"],
                        total_runs=row["total_runs"],
                        successful_runs=row["successful_runs"],
                        metadata=json.loads(row["metadata"] or "{}"),
                    )
            finally:
                conn.close()
        except Exception:
            pass

        return SourceHealthInfo(
            source_name=self.source_name,
            health=SourceHealth.UNKNOWN,
        )

    def _record_success(self) -> None:
        """Record a successful run."""
        self._health.last_success_at = self._now()
        self._health.consecutive_failures = 0
        self._health.total_runs += 1
        self._health.successful_runs += 1
        self._health.health = SourceHealth.HEALTHY
        self._save_health()

    def _record_failure(self, error: str) -> None:
        """Record a failed run."""
        self._health.last_failure_at = self._now()
        self._health.last_error = error
        self._health.consecutive_failures += 1
        self._health.total_runs += 1
        self._health.health = (
            SourceHealth.DEGRADED if self._health.consecutive_failures < 3
            else SourceHealth.FAILED
        )
        self._save_health()

    def _save_health(self) -> None:
        """Save health info to database."""
        try:
            conn = self.get_conn()
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO source_health
                       (source_name, status, last_success_at, last_failure_at,
                        last_error, consecutive_failures, total_runs,
                        successful_runs, failed_runs, avg_duration_seconds,
                        last_run_at, metadata, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        self.source_name,
                        self._health.health.value,
                        self._health.last_success_at,
                        self._health.last_failure_at,
                        self._health.last_error,
                        self._health.consecutive_failures,
                        self._health.total_runs,
                        self._health.successful_runs,
                        0,  # failed_runs - tracked separately in health monitor
                        0.0,  # avg_duration_seconds
                        self._health.last_success_at,
                        json.dumps(self._health.metadata),
                        self._now(),
                    )
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
