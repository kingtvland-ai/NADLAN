"""Deduplication layer for NADLANFIX.

Provides cross-source deduplication using:
1. Source-scoped external IDs (exact match within one source)
2. Property fingerprints (cross-source: city + street + price + rooms + area)

This module is the single implementation source for deduplication logic,
replacing scattered ad-hoc dedupe across dashboard.py and individual adapters.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Normalization for dedupe keys: strip spaces, tabs, non-breaking spaces,
# lowercase, remove everything except alphanumerics + Hebrew.
_DEDUPE_RE = re.compile(r"[^\dא-תa-z]+", re.IGNORECASE)


def _norm_key(value: Any) -> str:
    return _DEDUPE_RE.sub("", str(value or "").lower())


@dataclass
class DedupeResult:
    """Result of deduplication for a single listing."""
    canonical_id: str
    is_new: bool
    merged_sources: list[str]
    duplicate_of: str | None = None


class DedupeLayer:
    """Cross-source deduplication for NADLANFIX listings.

    Two identity kinds:
    1. **Source-scoped ID**: exact match of external_id within one source.
       Catches the same ad seen twice within one feed.
    2. **Property fingerprint**: cross-source match on city + street + price +
       rooms + area. Catches the same flat published on two boards.

    Within one board, identical fingerprints are NOT treated as duplicates:
    a developer can list seven identical units at one price, and the same flat
    is often advertised by two agencies (two leads, not one row to hide).
    Between boards, a matching fingerprint IS a duplicate.
    """

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create dedupe tables if they don't exist."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS canonical_aliases (
                    canonical_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    fingerprint TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    merged_sources TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY (canonical_id, source)
                );

                CREATE TABLE IF NOT EXISTS source_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_a TEXT NOT NULL,
                    external_id_a TEXT NOT NULL,
                    source_b TEXT NOT NULL,
                    external_id_b TEXT NOT NULL,
                    match_type TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 1.0,
                    created_at TEXT NOT NULL,
                    UNIQUE(source_a, external_id_a, source_b, external_id_b)
                );

                CREATE INDEX IF NOT EXISTS idx_canonical_source
                    ON canonical_aliases (source, external_id);
                CREATE INDEX IF NOT EXISTS idx_canonical_fingerprint
                    ON canonical_aliases (fingerprint);
                CREATE INDEX IF NOT EXISTS idx_source_links_a
                    ON source_links (source_a, external_id_a);
                CREATE INDEX IF NOT EXISTS idx_source_links_b
                    ON source_links (source_b, external_id_b);
            """)
            conn.commit()
        finally:
            conn.close()

    def resolve(self, source: str, external_id: str, fingerprint: str | None = None) -> DedupeResult:
        """Resolve a listing to its canonical ID.

        Args:
            source: Source name (e.g., 'yad2', 'facebook')
            external_id: Original ID from the source
            fingerprint: Optional property fingerprint

        Returns:
            DedupeResult with canonical_id and merge info
        """
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")

            # 1. Check exact ID match within same source
            row = conn.execute(
                "SELECT canonical_id, merged_sources FROM canonical_aliases "
                "WHERE source = ? AND external_id = ?",
                (source, external_id)
            ).fetchone()

            if row:
                # Update last_seen
                conn.execute(
                    "UPDATE canonical_aliases SET last_seen_at = ? "
                    "WHERE canonical_id = ? AND source = ?",
                    (now, row["canonical_id"], source)
                )
                conn.commit()
                merged = json.loads(row["merged_sources"] or "[]")
                return DedupeResult(
                    canonical_id=row["canonical_id"],
                    is_new=False,
                    merged_sources=merged,
                )

            # 2. Check fingerprint match across different sources
            if fingerprint:
                row = conn.execute(
                    "SELECT canonical_id, source, external_id, merged_sources FROM canonical_aliases "
                    "WHERE fingerprint = ? AND source != ?",
                    (fingerprint, source)
                ).fetchone()

                if row:
                    # Link this source's ID to the existing canonical ID
                    merged = json.loads(row["merged_sources"] or "[]")
                    if source not in merged:
                        merged.append(source)
                        conn.execute(
                            "UPDATE canonical_aliases SET merged_sources = ?, "
                            "last_seen_at = ? WHERE canonical_id = ?",
                            (json.dumps(merged), now, row["canonical_id"])
                        )
                        conn.commit()

                    # Record the link
                    conn.execute(
                        "INSERT OR IGNORE INTO source_links "
                        "(source_a, external_id_a, source_b, external_id_b, match_type, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (source, external_id, row["source"],
                         row["external_id"], "fingerprint", now)
                    )
                    conn.commit()

                    return DedupeResult(
                        canonical_id=row["canonical_id"],
                        is_new=False,
                        merged_sources=merged,
                        duplicate_of=f"{row['source']}:{row['external_id']}",
                    )

            # 3. New listing - create canonical ID
            canonical_id = f"{source}:{external_id}"
            merged_sources = [source]

            conn.execute(
                "INSERT INTO canonical_aliases "
                "(canonical_id, source, external_id, fingerprint, first_seen_at, last_seen_at, merged_sources) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (canonical_id, source, external_id, fingerprint, now, now, json.dumps(merged_sources))
            )
            conn.commit()

            return DedupeResult(
                canonical_id=canonical_id,
                is_new=True,
                merged_sources=merged_sources,
            )

        finally:
            conn.close()

    def get_aliases(self, canonical_id: str) -> list[dict]:
        """Get all source aliases for a canonical listing."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM canonical_aliases WHERE canonical_id = ?",
                (canonical_id,)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_links(self, source: str, external_id: str) -> list[dict]:
        """Get all links for a listing."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM source_links "
                "WHERE (source_a = ? AND external_id_a = ?) "
                "   OR (source_b = ? AND external_id_b = ?)",
                (source, external_id, source, external_id)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def count(self) -> dict:
        """Return dedupe statistics."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            canonical = conn.execute("SELECT COUNT(*) FROM canonical_aliases").fetchone()[0]
            links = conn.execute("SELECT COUNT(*) FROM source_links").fetchone()[0]
            sources = conn.execute(
                "SELECT source, COUNT(*) as cnt FROM canonical_aliases GROUP BY source"
            ).fetchall()
            return {
                "canonical_listings": canonical,
                "cross_source_links": links,
                "by_source": {row[0]: row[1] for row in sources},
            }
        finally:
            conn.close()
