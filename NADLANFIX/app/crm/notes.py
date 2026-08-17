"""CRM Notes module.

Provides notes business logic and API endpoints.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from app.crm.crm_backend import CRMBackend, Note


@dataclass
class NoteCreate:
    """Note creation payload."""
    lead_id: int
    content: str
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class NoteService:
    """Note business logic service."""

    def __init__(self, db_path):
        self.crm = CRMBackend(db_path)

    def create(self, payload: NoteCreate, user_id: int) -> dict:
        """Create a new note."""
        note = Note(
            note_id=0,
            lead_id=payload.lead_id,
            content=payload.content,
            created_by=user_id,
            tags=payload.tags,
            metadata=payload.metadata,
        )
        note_id = self.crm.create_note(note)
        return {"note_id": note_id, "status": "created"}

    def get(self, note_id: int) -> dict | None:
        """Get a note by ID."""
        conn = sqlite3.connect(str(self.crm.db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM crm_notes WHERE note_id = ?", (note_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def list_for_lead(self, lead_id: int, limit: int = 50, offset: int = 0) -> list[dict]:
        """Get notes for a lead."""
        return self.crm.get_notes(lead_id, limit=limit, offset=offset)

    def delete(self, note_id: int, user_id: int) -> bool:
        """Delete a note."""
        conn = sqlite3.connect(str(self.crm.db_path))
        try:
            cursor = conn.execute("DELETE FROM crm_notes WHERE note_id = ?", (note_id,))
            if cursor.rowcount > 0:
                self.crm._log_activity(conn, "note", note_id, "deleted", user_id=user_id)
                conn.commit()
                return True
            return False
        finally:
            conn.close()
