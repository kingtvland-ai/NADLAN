"""Firestore sync layer for NADLANFIX.

Syncs active listings and operational data from SQLite to Firestore.
Firestore is the read-optimized operational DB; SQLite is the raw archive.

Collections:
- listings: active listings for client consumption
- source_runs: run summaries
- source_health: health status per source
- alerts: active alerts
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


class FirestoreSyncError(Exception):
    """Error during Firestore sync."""
    pass


class FirestoreSync:
    """Syncs NADLANFIX data to Firestore."""

    def __init__(self, db_path: Path | str, project_id: str | None = None):
        self.db_path = Path(db_path)
        self.project_id = project_id
        self._client = None
        self._ensure_sync_schema()

    def _ensure_sync_schema(self) -> None:
        """Create sync tracking tables if they don't exist."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS sync_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collection TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 5,
                    last_error TEXT,
                    next_attempt_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS sync_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collection TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    duration_ms REAL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE INDEX IF NOT EXISTS idx_sync_queue_next_attempt ON sync_queue (next_attempt_at);
                CREATE INDEX IF NOT EXISTS idx_sync_history_collection ON sync_history (collection, created_at);
            """)
            conn.commit()
        finally:
            conn.close()

    @property
    def client(self):
        """Lazy-load Firestore client."""
        if not HAS_FIRESTORE:
            raise FirestoreSyncError(
                "google-cloud-firestore is not installed. "
                "Install it with: pip install google-cloud-firestore"
            )
        if self._client is None:
            from google.cloud import firestore
            self._client = firestore.Client(project=self.project_id)
        return self._client

    def sync_listings(self, batch_size: int = 500) -> dict:
        """Sync all active listings from normalized_listings to Firestore.

        Uses batch upserts with per-document error handling and retry queue.
        After syncing active listings, deletes stale ones from Firestore.

        Args:
            batch_size: Number of records per batch

        Returns:
            Sync result summary
        """
        if not HAS_FIRESTORE:
            return {"error": "Firestore not available", "synced": 0, "deleted": 0, "failed": 0}

        collection = self.client.collection("listings")
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row

        try:
            rows = conn.execute(
                "SELECT * FROM normalized_listings WHERE is_active = 1 ORDER BY last_seen_at DESC"
            ).fetchall()

            synced = 0
            failed = 0
            batch = self.client.batch()
            active_ids = set()
            batch_count = 0

            for row in rows:
                doc_ref = collection.document(row["canonical_id"])
                data = self._listing_to_firestore(dict(row))

                try:
                    self._idempotent_set(doc_ref, data)
                    active_ids.add(row["canonical_id"])
                    synced += 1
                    batch_count += 1

                    if batch_count % batch_size == 0:
                        batch.commit()
                        batch = self.client.batch()

                except Exception as exc:
                    failed += 1
                    self._queue_sync("listings", row["canonical_id"], "set", data)
                    self._record_sync_history("listings", row["canonical_id"], "set",
                                              "failed", error=str(exc))

            try:
                batch.commit()
            except Exception:
                pass

            deleted = self._delete_stale_listings(collection, active_ids)

            return {
                "synced": synced,
                "deleted": deleted,
                "total_active": len(active_ids),
                "failed": failed,
            }

        finally:
            conn.close()

    def process_sync_queue(self, max_items: int = 100) -> dict:
        """Process failed sync operations from the retry queue.

        Args:
            max_items: Maximum number of items to process

        Returns:
            Processing result summary
        """
        if not HAS_FIRESTORE:
            return {"error": "Firestore not available", "processed": 0, "succeeded": 0, "failed": 0}

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        succeeded = 0
        failed = 0

        try:
            rows = conn.execute(
                """SELECT * FROM sync_queue
                   WHERE attempts < max_attempts
                   AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                   ORDER BY created_at ASC
                   LIMIT ?""",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), max_items)
            ).fetchall()

            for row in rows:
                try:
                    payload = json.loads(row["payload"])
                    doc_ref = self.client.collection(row["collection"]).document(row["document_id"])

                    if row["operation"] == "set":
                        self._idempotent_set(doc_ref, payload)
                    elif row["operation"] == "delete":
                        doc_ref.delete()

                    self._record_sync_history(row["collection"], row["document_id"],
                                              row["operation"], "success")
                    conn.execute("DELETE FROM sync_queue WHERE id = ?", (row["id"],))
                    succeeded += 1

                except Exception as exc:
                    attempts = row["attempts"] + 1
                    next_attempt = datetime.now(timezone.utc) + timezone.timedelta(
                        seconds=min(300, 30 * (2 ** attempts))
                    )
                    conn.execute(
                        """UPDATE sync_queue
                           SET attempts = ?, last_error = ?, next_attempt_at = ?, updated_at = ?
                           WHERE id = ?""",
                        (attempts, str(exc), next_attempt.isoformat(timespec="seconds"),
                         datetime.now(timezone.utc).isoformat(timespec="seconds"), row["id"])
                    )
                    self._record_sync_history(row["collection"], row["document_id"],
                                              row["operation"], "failed", error=str(exc))
                    failed += 1

            conn.commit()
            return {"processed": len(rows), "succeeded": succeeded, "failed": failed}

        finally:
            conn.close()

    def _listing_to_firestore(self, row: dict) -> dict:
        """Convert a normalized listing row to Firestore document."""
        return {
            "canonical_id": row["canonical_id"],
            "source": row["source"],
            "external_id": row["external_id"],
            "title": row["title"],
            "description": row.get("description"),
            "price": row.get("price"),
            "price_text": row.get("price_text"),
            "currency": row.get("currency", "ILS"),
            "property_type": row.get("property_type", "other"),
            "condition": row.get("condition", "unknown"),
            "rooms": row.get("rooms"),
            "area_sqm": row.get("area_sqm"),
            "floor": row.get("floor"),
            "city": row["city"],
            "neighborhood": row.get("neighborhood"),
            "street": row.get("street", ""),
            "address_text": row.get("address_text", ""),
            "lat": row.get("lat"),
            "lon": row.get("lon"),
            "gush": row.get("gush"),
            "helka": row.get("helka"),
            "image_url": row.get("image_url"),
            "images": json.loads(row["images_json"] or "[]"),
            "url": row.get("url"),
            "category": row.get("category", "other"),
            "deal_score": row.get("deal_score", 0.0),
            "score_reasons": json.loads(row["score_reasons_json"] or "[]"),
            "seller_name": row.get("seller_name"),
            "agency": row.get("agency"),
            "phone": row.get("phone"),
            "first_seen_at": row.get("first_seen_at"),
            "last_seen_at": row.get("last_seen_at"),
            "is_active": True,
            "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def _delete_stale_listings(self, collection, active_ids: set[str]) -> int:
        """Delete Firestore documents not in active_ids.

        Args:
            collection: Firestore collection reference
            active_ids: Set of currently active canonical IDs

        Returns:
            Number of deleted documents
        """
        deleted = 0
        batch = self.client.batch()

        for doc in collection.stream():
            if doc.id not in active_ids:
                batch.delete(doc.reference)
                deleted += 1
                if deleted % 500 == 0:
                    batch.commit()
                    batch = self.client.batch()

        batch.commit()
        return deleted

    def sync_health(self, health_data: dict) -> None:
        """Sync source health to Firestore."""
        if not HAS_FIRESTORE:
            return

        doc_ref = self.client.collection("source_health").document(health_data["source_name"])
        health_data["synced_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        doc_ref.set(health_data, merge=True)

    def sync_run_summary(self, run_data: dict) -> None:
        """Sync a run summary to Firestore."""
        if not HAS_FIRESTORE:
            return

        doc_ref = self.client.collection("source_runs").document()
        run_data["synced_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        doc_ref.set(run_data)

    def get_active_listings(self, source: str | None = None, limit: int = 1000) -> list[dict]:
        """Get active listings from Firestore.

        Args:
            source: Optional source filter
            limit: Maximum number of listings to return

        Returns:
            List of listing dicts
        """
        if not HAS_FIRESTORE:
            return []

        collection = self.client.collection("listings")
        query = collection.where("is_active", "==", True).limit(limit)

        if source:
            query = query.where("source", "==", source)

        return [doc.to_dict() for doc in query.stream()]

    # -- Retry queue --

    def _queue_sync(self, collection: str, document_id: str, operation: str, payload: dict) -> None:
        """Queue a sync operation for retry."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                """INSERT INTO sync_queue (collection, document_id, operation, payload)
                   VALUES (?, ?, ?, ?)""",
                (collection, document_id, operation, json.dumps(payload)),
            )
            conn.commit()
        finally:
            conn.close()

    def _record_sync_history(self, collection: str, document_id: str, operation: str,
                             status: str, error: str | None = None, duration_ms: float | None = None) -> None:
        """Record sync operation in history."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                """INSERT INTO sync_history (collection, document_id, operation, status, error, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (collection, document_id, operation, status, error, duration_ms),
            )
            conn.commit()
        finally:
            conn.close()

    def get_sync_history(self, collection: str | None = None, limit: int = 100) -> list[dict]:
        """Get sync history."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            query = "SELECT * FROM sync_history WHERE 1=1"
            params = []
            if collection:
                query += " AND collection = ?"
                params.append(collection)
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def _idempotent_set(self, doc_ref, data: dict) -> None:
        """Idempotent set: only write if data changed."""
        existing = doc_ref.get()
        if existing.exists:
            existing_data = existing.to_dict()
            if existing_data.get("synced_at") and all(
                existing_data.get(k) == v for k, v in data.items() if k != "synced_at"
            ):
                return
        doc_ref.set(data, merge=True)

    # -- CRM sync --

    def sync_crm_data(self, db_path: Path | str | None = None) -> dict:
        """Sync CRM data to Firestore.

        Args:
            db_path: Path to SQLite database (defaults to self.db_path)

        Returns:
            Sync result summary
        """
        if not HAS_FIRESTORE:
            return {"error": "Firestore not available", "leads_synced": 0, "opportunities_synced": 0}

        source_db = Path(db_path) if db_path else self.db_path
        conn = sqlite3.connect(str(source_db))
        conn.row_factory = sqlite3.Row
        leads_synced = 0
        opportunities_synced = 0

        try:
            leads_collection = self.client.collection("crm_leads")
            rows = conn.execute("SELECT * FROM crm_leads ORDER BY updated_at DESC").fetchall()
            for row in rows:
                lead = dict(row)
                doc_ref = leads_collection.document(str(lead["lead_id"]))
                data = {
                    "lead_id": lead["lead_id"],
                    "title": lead["title"],
                    "description": lead.get("description"),
                    "status": lead["status"],
                    "source": lead["source"],
                    "listing_id": lead.get("listing_id"),
                    "canonical_id": lead.get("canonical_id"),
                    "city": lead.get("city"),
                    "neighborhood": lead.get("neighborhood"),
                    "price": lead.get("price"),
                    "rooms": lead.get("rooms"),
                    "area_sqm": lead.get("area_sqm"),
                    "contact_name": lead.get("contact_name"),
                    "phone": lead.get("phone"),
                    "email": lead.get("email"),
                    "owner_id": lead.get("owner_id"),
                    "assigned_to": lead.get("assigned_to"),
                    "last_contact_at": lead.get("last_contact_at"),
                    "next_action_at": lead.get("next_action_at"),
                    "tags": json.loads(lead.get("tags") or "[]"),
                    "metadata": json.loads(lead.get("metadata") or "{}"),
                    "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                self._idempotent_set(doc_ref, data)
                leads_synced += 1

            opportunities_collection = self.client.collection("crm_opportunities")
            rows = conn.execute("SELECT * FROM crm_opportunities ORDER BY updated_at DESC").fetchall()
            for row in rows:
                opp = dict(row)
                doc_ref = opportunities_collection.document(str(opp["opportunity_id"]))
                data = {
                    "opportunity_id": opp["opportunity_id"],
                    "lead_id": opp["lead_id"],
                    "title": opp["title"],
                    "description": opp.get("description"),
                    "value": opp.get("value"),
                    "probability": opp.get("probability", 0.0),
                    "stage": opp["stage"],
                    "expected_close_at": opp.get("expected_close_at"),
                    "actual_close_at": opp.get("actual_close_at"),
                    "won": bool(opp.get("won", 0)),
                    "lost_reason": opp.get("lost_reason"),
                    "created_by": opp.get("created_by"),
                    "assigned_to": opp.get("assigned_to"),
                    "tags": json.loads(opp.get("tags") or "[]"),
                    "metadata": json.loads(opp.get("metadata") or "{}"),
                    "synced_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                self._idempotent_set(doc_ref, data)
                opportunities_synced += 1

            return {
                "leads_synced": leads_synced,
                "opportunities_synced": opportunities_synced,
            }
        finally:
            conn.close()
