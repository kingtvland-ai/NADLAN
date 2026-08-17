"""Snapshot system for NADLANFIX.

Stores daily snapshots of raw and normalized data to prevent degradation
over time and enable historical analysis.
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
import tarfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))
from sources.base.adapter import BaseSourceAdapter


@dataclass
class Snapshot:
    """Represents a data snapshot."""
    snapshot_id: str
    snapshot_type: str  # 'raw', 'normalized', 'export'
    source_name: str
    created_at: str
    file_path: str
    size_bytes: int
    record_count: int
    metadata: dict = field(default_factory=dict)


class SnapshotManager:
    """Manages data snapshots for NADLANFIX."""

    SNAPSHOT_TYPES = ("raw", "normalized", "export")

    def __init__(self, db_path: Path | str, snapshot_dir: Path | str | None = None):
        self.db_path = Path(db_path)
        self.snapshot_dir = Path(snapshot_dir) if snapshot_dir else self.db_path.parent / "snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create snapshot tables if they don't exist."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    snapshot_type TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    record_count INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT,
                    FOREIGN KEY (source_name) REFERENCES sources(source_name)
                );

                CREATE INDEX IF NOT EXISTS idx_snapshots_source
                    ON snapshots (source_name, created_at);
                CREATE INDEX IF NOT EXISTS idx_snapshots_type
                    ON snapshots (snapshot_type, created_at);
            """)
            conn.commit()
        finally:
            conn.close()

    def create_snapshot(self, source_adapter: BaseSourceAdapter, snapshot_type: str = "raw") -> Snapshot:
        """Create a snapshot of source data.

        Args:
            source_adapter: Source adapter to snapshot
            snapshot_type: Type of snapshot ('raw', 'normalized', 'export')

        Returns:
            Snapshot metadata
        """
        if snapshot_type not in self.SNAPSHOT_TYPES:
            raise ValueError(f"Invalid snapshot type: {snapshot_type}")

        source_name = source_adapter.source_name
        snapshot_id = f"{source_name}_{snapshot_type}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

        # Create snapshot file
        snapshot_file = self.snapshot_dir / f"{snapshot_id}.tar.gz"
        conn = source_adapter.get_conn()

        try:
            # Export data to snapshot
            with tarfile.open(snapshot_file, "w:gz") as tar:
                # Export listings
                rows = conn.execute(
                    "SELECT * FROM {}_listings".format(source_name)
                ).fetchall()

                listings_json = json.dumps([dict(row) for row in rows], ensure_ascii=False)
                info = tarfile.TarInfo(name="listings.json")
                info.size = len(listings_json.encode("utf-8"))
                tar.addfile(info, fileobj=io.BytesIO(listings_json.encode("utf-8")))

                # Export metadata
                metadata = {
                    "source_name": source_name,
                    "snapshot_type": snapshot_type,
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "record_count": len(rows),
                }
                metadata_json = json.dumps(metadata, ensure_ascii=False)
                info = tarfile.TarInfo(name="metadata.json")
                info.size = len(metadata_json.encode("utf-8"))
                tar.addfile(info, fileobj=io.BytesIO(metadata_json.encode("utf-8")))

            # Record in database
            snapshot = Snapshot(
                snapshot_id=snapshot_id,
                snapshot_type=snapshot_type,
                source_name=source_name,
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                file_path=str(snapshot_file),
                size_bytes=snapshot_file.stat().st_size,
                record_count=len(rows),
                metadata=metadata,
            )

            self._save_snapshot(snapshot)
            return snapshot

        finally:
            conn.close()

    def get_snapshots(self, source_name: str | None = None, snapshot_type: str | None = None) -> list[Snapshot]:
        """Get snapshots, optionally filtered.

        Args:
            source_name: Optional source filter
            snapshot_type: Optional type filter

        Returns:
            List of snapshots
        """
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            query = "SELECT * FROM snapshots WHERE 1=1"
            params = []

            if source_name:
                query += " AND source_name = ?"
                params.append(source_name)

            if snapshot_type:
                query += " AND snapshot_type = ?"
                params.append(snapshot_type)

            query += " ORDER BY created_at DESC"

            rows = conn.execute(query, params).fetchall()
            return [
                Snapshot(
                    snapshot_id=row["snapshot_id"],
                    snapshot_type=row["snapshot_type"],
                    source_name=row["source_name"],
                    created_at=row["created_at"],
                    file_path=row["file_path"],
                    size_bytes=row["size_bytes"],
                    record_count=row["record_count"],
                    metadata=json.loads(row["metadata"] or "{}"),
                )
                for row in rows
            ]
        finally:
            conn.close()

    def cleanup_old_snapshots(self, keep_days: int = 30) -> int:
        """Remove snapshots older than keep_days.

        Args:
            keep_days: Number of days to keep

        Returns:
            Number of snapshots removed
        """
        cutoff = datetime.now(timezone.utc).timestamp() - (keep_days * 24 * 3600)
        snapshots = self.get_snapshots()

        removed = 0
        for snap in snapshots:
            created = datetime.fromisoformat(snap.created_at).timestamp()
            if created < cutoff:
                # Remove file
                try:
                    Path(snap.file_path).unlink(missing_ok=True)
                except Exception:
                    pass

                # Remove from database
                conn = sqlite3.connect(str(self.db_path))
                try:
                    conn.execute("DELETE FROM snapshots WHERE snapshot_id = ?", (snap.snapshot_id,))
                    conn.commit()
                    removed += 1
                finally:
                    conn.close()

        return removed

    def _save_snapshot(self, snapshot: Snapshot) -> None:
        """Save snapshot metadata to database."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                """INSERT INTO snapshots
                   (snapshot_id, snapshot_type, source_name, created_at,
                    file_path, size_bytes, record_count, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot.snapshot_id,
                    snapshot.snapshot_type,
                    snapshot.source_name,
                    snapshot.created_at,
                    snapshot.file_path,
                    snapshot.size_bytes,
                    snapshot.record_count,
                    json.dumps(snapshot.metadata),
                )
            )
            conn.commit()
        finally:
            conn.close()
