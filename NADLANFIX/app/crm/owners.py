"""CRM Owners module.

Provides ownership and assignment business logic.
"""

from __future__ import annotations

from typing import Any

from app.crm.crm_backend import CRMBackend


class OwnerService:
    """Ownership and assignment service."""

    def __init__(self, db_path):
        self.crm = CRMBackend(db_path)

    def get_owner(self, lead_id: int) -> dict | None:
        """Get the owner of a lead."""
        lead = self.crm.get_lead(lead_id)
        if not lead:
            return None
        return {
            "lead_id": lead_id,
            "owner_id": lead.get("owner_id"),
            "assigned_to": lead.get("assigned_to"),
        }

    def transfer_ownership(self, lead_id: int, new_owner_id: int | None, user_id: int) -> bool:
        """Transfer lead ownership."""
        return self.crm.assign_lead(lead_id, new_owner_id, user_id)

    def get_team_leads(self, user_id: int, limit: int = 100) -> list[dict]:
        """Get leads owned by or assigned to a user's team."""
        owned = self.crm.list_leads(assigned_to=user_id, limit=limit)
        return owned

    def get_unassigned_leads(self, limit: int = 100) -> list[dict]:
        """Get leads without an assigned owner."""
        conn = self.crm.db_path if hasattr(self.crm, 'db_path') else None
        import sqlite3
        conn = sqlite3.connect(str(self.crm.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM crm_leads WHERE assigned_to IS NULL ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
