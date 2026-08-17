"""Schema versioning for NADLANFIX.

Tracks database schema versions and provides migration utilities.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


SCHEMA_VERSION = "1.0.0"
APP_VERSION = "nadlanfix-2025.1"


class SchemaVersioning:
    """Manages database schema versions."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create schema version table if it doesn't exist."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS schema_versions (
                    version TEXT PRIMARY KEY,
                    app_version TEXT NOT NULL,
                    description TEXT,
                    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS app_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
            """)
            conn.commit()
        finally:
            conn.close()

    def get_current_version(self) -> Optional[str]:
        """Get the current schema version."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            row = conn.execute(
                "SELECT version FROM schema_versions ORDER BY applied_at DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def set_version(self, version: str, description: str = "") -> None:
        """Set the current schema version."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "INSERT OR REPLACE INTO app_metadata (key, value) VALUES (?, ?)",
                ("schema_version", version)
            )
            conn.execute(
                "INSERT OR REPLACE INTO app_metadata (key, value) VALUES (?, ?)",
                ("app_version", APP_VERSION)
            )
            conn.execute(
                "INSERT INTO schema_versions (version, app_version, description) VALUES (?, ?, ?)",
                (version, APP_VERSION, description)
            )
            conn.commit()
        finally:
            conn.close()

    def get_metadata(self, key: str) -> Optional[str]:
        """Get app metadata value."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            row = conn.execute(
                "SELECT value FROM app_metadata WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def set_metadata(self, key: str, value: str) -> None:
        """Set app metadata value."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "INSERT OR REPLACE INTO app_metadata (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, datetime.now(timezone.utc).isoformat(timespec="seconds"))
            )
            conn.commit()
        finally:
            conn.close()

    def ensure_version(self) -> str:
        """Ensure the database has a schema version recorded."""
        current = self.get_current_version()
        if current is None:
            self.set_version(SCHEMA_VERSION, "Initial schema")
            return SCHEMA_VERSION
        return current
