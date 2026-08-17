"""Backup and restore utilities for NADLANFIX.

Provides:
- Database backup
- Snapshot management
- Restore procedures
- Backup retention policies
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


@dataclass
class BackupInfo:
    """Backup metadata."""
    backup_id: str
    backup_type: str  # 'full', 'incremental', 'snapshot'
    created_at: str
    file_path: str
    size_bytes: int
    database_path: str
    tables: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class BackupManager:
    """Manages database backups for NADLANFIX."""

    def __init__(self, db_path: Path | str, backup_dir: Path | str | None = None):
        self.db_path = Path(db_path)
        self.backup_dir = Path(backup_dir) if backup_dir else self.db_path.parent / "backups"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create backup tracking tables if they don't exist."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS backup_history (
                    backup_id TEXT PRIMARY KEY,
                    backup_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    database_path TEXT NOT NULL,
                    tables TEXT,
                    metadata TEXT,
                    restored_at TEXT,
                    is_restored INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_backup_history_created
                    ON backup_history (created_at);
            """)
            conn.commit()
        finally:
            conn.close()

    def create_backup(self, backup_type: str = "full", tables: list[str] | None = None) -> BackupInfo:
        """Create a database backup.

        Args:
            backup_type: Type of backup ('full', 'incremental', 'snapshot')
            tables: List of tables to backup (None for all)

        Returns:
            BackupInfo with backup details
        """
        backup_id = f"backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{backup_type}"
        backup_file = self.backup_dir / f"{backup_id}.tar.gz"

        # Get list of tables
        conn = sqlite3.connect(str(self.db_path))
        try:
            if tables is None:
                tables = [row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()]

            # Create backup
            with tarfile.open(backup_file, "w:gz") as tar:
                # Backup database file
                tar.add(self.db_path, arcname=self.db_path.name)

                # Backup metadata
                metadata = {
                    "backup_type": backup_type,
                    "tables": tables,
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "database_path": str(self.db_path),
                }
                metadata_json = json.dumps(metadata, ensure_ascii=False)
                info = tarfile.TarInfo(name="backup_metadata.json")
                info.size = len(metadata_json.encode("utf-8"))
                tar.addfile(info, fileobj=__import__("io").BytesIO(metadata_json.encode("utf-8")))

            backup_info = BackupInfo(
                backup_id=backup_id,
                backup_type=backup_type,
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                file_path=str(backup_file),
                size_bytes=backup_file.stat().st_size,
                database_path=str(self.db_path),
                tables=tables,
                metadata=metadata,
            )

            self._record_backup(backup_info)
            return backup_info

        finally:
            conn.close()

    def restore_backup(self, backup_id: str, target_path: Path | str | None = None) -> bool:
        """Restore a backup.

        Args:
            backup_id: ID of the backup to restore
            target_path: Target path for restored database (None to overwrite current)

        Returns:
            True if successful
        """
        conn = sqlite3.connect(str(self.db_path))
        try:
            row = conn.execute(
                "SELECT * FROM backup_history WHERE backup_id = ?", (backup_id,)
            ).fetchone()
            if not row:
                return False

            backup_file = Path(row["file_path"])
            if not backup_file.exists():
                return False

            target = Path(target_path) if target_path else self.db_path

            # Create backup of current database before restore
            if target.exists():
                backup_before = target.with_suffix(f".before_restore_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.sqlite3")
                shutil.copy2(target, backup_before)

            # Restore from backup
            with tarfile.open(backup_file, "r:gz") as tar:
                # Extract database file
                db_member = None
                for member in tar.getmembers():
                    if member.name.endswith(".sqlite3"):
                        db_member = member
                        break

                if db_member:
                    tar.extract(db_member, path=target.parent)
                    extracted = target.parent / db_member.name
                    if extracted != target:
                        shutil.move(extracted, target)

            # Mark as restored
            conn.execute(
                "UPDATE backup_history SET is_restored = 1, restored_at = ? WHERE backup_id = ?",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), backup_id)
            )
            conn.commit()
            return True

        finally:
            conn.close()

    def list_backups(self, limit: int = 50) -> list[BackupInfo]:
        """List available backups."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM backup_history ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [
                BackupInfo(
                    backup_id=row["backup_id"],
                    backup_type=row["backup_type"],
                    created_at=row["created_at"],
                    file_path=row["file_path"],
                    size_bytes=row["size_bytes"],
                    database_path=row["database_path"],
                    tables=json.loads(row["tables"] or "[]"),
                    metadata=json.loads(row["metadata"] or "{}"),
                )
                for row in rows
            ]
        finally:
            conn.close()

    def cleanup_old_backups(self, keep_days: int = 30) -> int:
        """Remove backups older than keep_days.

        Args:
            keep_days: Number of days to keep

        Returns:
            Number of backups removed
        """
        cutoff = datetime.now(timezone.utc).timestamp() - (keep_days * 24 * 3600)
        backups = self.list_backups(limit=1000)

        removed = 0
        for backup in backups:
            created = datetime.fromisoformat(backup.created_at).timestamp()
            if created < cutoff:
                # Remove file
                try:
                    Path(backup.file_path).unlink(missing_ok=True)
                except Exception:
                    pass

                # Remove from database
                conn = sqlite3.connect(str(self.db_path))
                try:
                    conn.execute("DELETE FROM backup_history WHERE backup_id = ?", (backup.backup_id,))
                    conn.commit()
                    removed += 1
                finally:
                    conn.close()

        return removed

    def _record_backup(self, backup_info: BackupInfo) -> None:
        """Record backup in database."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                """INSERT INTO backup_history
                   (backup_id, backup_type, created_at, file_path, size_bytes,
                    database_path, tables, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    backup_info.backup_id,
                    backup_info.backup_type,
                    backup_info.created_at,
                    backup_info.file_path,
                    backup_info.size_bytes,
                    backup_info.database_path,
                    json.dumps(backup_info.tables),
                    json.dumps(backup_info.metadata),
                )
            )
            conn.commit()
        finally:
            conn.close()
