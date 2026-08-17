"""Active DB reader for NADLANFIX.

Provides read-optimized access to the active database (Firestore).
Falls back to SQLite if Firestore is not available.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from google.cloud import firestore
    HAS_FIRESTORE = True
except ImportError:
    HAS_FIRESTORE = False


class ActiveDBError(Exception):
    """Error during active DB read."""
    pass


class ActiveDBReader:
    """Read-optimized access to the active database.

    Reads from Firestore when available, falls back to SQLite.
    """

    def __init__(self, db_path: Path | str, project_id: str | None = None):
        self.db_path = Path(db_path)
        self.project_id = project_id
        self._client = None

    @property
    def client(self):
        """Lazy-load Firestore client."""
        if not HAS_FIRESTORE:
            raise ActiveDBError("Firestore not available")
        if self._client is None:
            from google.cloud import firestore
            self._client = firestore.Client(project=self.project_id)
        return self._client

    def get_active_listings(self, source: str | None = None, limit: int = 1000) -> list[dict]:
        """Get active listings from the active DB.

        Args:
            source: Optional source filter
            limit: Maximum number of listings to return

        Returns:
            List of listing dicts
        """
        if HAS_FIRESTORE:
            try:
                return self._get_listings_from_firestore(source, limit)
            except Exception:
                pass

        return self._get_listings_from_sqlite(source, limit)

    def _get_listings_from_firestore(self, source: str | None, limit: int) -> list[dict]:
        """Get listings from Firestore."""
        collection = self.client.collection("listings")
        query = collection.where("is_active", "==", True).limit(limit)

        if source:
            query = query.where("source", "==", source)

        return [doc.to_dict() for doc in query.stream()]

    def _get_listings_from_sqlite(self, source: str | None, limit: int) -> list[dict]:
        """Get listings from SQLite as fallback."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            query = "SELECT * FROM normalized_listings WHERE is_active = 1"
            params = []
            if source:
                query += " AND source = ?"
                params.append(source)
            query += " ORDER BY last_seen_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_listing(self, canonical_id: str) -> dict | None:
        """Get a single listing by canonical ID.

        Args:
            canonical_id: The canonical listing ID

        Returns:
            Listing dict or None
        """
        if HAS_FIRESTORE:
            try:
                doc = self.client.collection("listings").document(canonical_id).get()
                if doc.exists:
                    return doc.to_dict()
            except Exception:
                pass

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM normalized_listings WHERE canonical_id = ? AND is_active = 1",
                (canonical_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_crm_leads(self, limit: int = 1000) -> list[dict]:
        """Get CRM leads from the active DB.

        Args:
            limit: Maximum number of leads to return

        Returns:
            List of lead dicts
        """
        if HAS_FIRESTORE:
            try:
                docs = self.client.collection("crm_leads").limit(limit).stream()
                return [doc.to_dict() for doc in docs]
            except Exception:
                pass

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM crm_leads ORDER BY updated_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_crm_opportunities(self, limit: int = 1000) -> list[dict]:
        """Get CRM opportunities from the active DB.

        Args:
            limit: Maximum number of opportunities to return

        Returns:
            List of opportunity dicts
        """
        if HAS_FIRESTORE:
            try:
                docs = self.client.collection("crm_opportunities").limit(limit).stream()
                return [doc.to_dict() for doc in docs]
            except Exception:
                pass

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM crm_opportunities ORDER BY updated_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def health(self) -> dict:
        """Check active DB health."""
        status = {
            "firestore_available": HAS_FIRESTORE,
            "sqlite_available": self.db_path.exists(),
            "active_source": "firestore" if HAS_FIRESTORE else "sqlite",
        }

        if HAS_FIRESTORE:
            try:
                self.client.collection("listings").limit(1).get()
                status["firestore_healthy"] = True
            except Exception:
                status["firestore_healthy"] = False
                status["active_source"] = "sqlite"

        return status
