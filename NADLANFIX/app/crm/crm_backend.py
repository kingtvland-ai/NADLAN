"""CRM Backend for NADLANFIX.

Provides lead management, pipelines, tasks, and notes.
This is the operational backend for agents and managers.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class LeadStatus(Enum):
    """Lead lifecycle stages."""
    NEW = "new"
    CONTACTED = "contacted"
    QUALIFIED = "qualified"
    PROPOSAL = "proposal"
    NEGOTIATION = "negotiation"
    WON = "won"
    LOST = "lost"


class TaskStatus(Enum):
    """Task status."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TaskPriority(Enum):
    """Task priority."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


@dataclass
class Lead:
    """CRM Lead."""
    lead_id: int
    title: str
    description: str
    status: LeadStatus
    source: str  # 'yad2', 'facebook', 'onmap', 'manual'
    listing_id: Optional[str] = None
    canonical_id: Optional[str] = None
    city: Optional[str] = None
    neighborhood: Optional[str] = None
    price: Optional[float] = None
    rooms: Optional[float] = None
    area_sqm: Optional[float] = None
    contact_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    owner_id: Optional[int] = None
    assigned_to: Optional[int] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    last_contact_at: Optional[str] = None
    next_action_at: Optional[str] = None
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class Task:
    """CRM Task."""
    task_id: int
    title: str
    description: str
    status: TaskStatus
    priority: TaskPriority
    created_by: int
    lead_id: Optional[int] = None
    assigned_to: Optional[int] = None
    due_at: Optional[str] = None
    completed_at: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class Note:
    """CRM Note."""
    note_id: int
    lead_id: int
    content: str
    created_by: int
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class Pipeline:
    """CRM Pipeline."""
    pipeline_id: int
    name: str
    description: str
    stages: list[dict]
    is_default: bool = False
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))


@dataclass
class Opportunity:
    """CRM Opportunity."""
    opportunity_id: int
    lead_id: int
    title: str
    description: str
    value: float | None = None
    probability: float = 0.0
    stage: str = "qualification"
    expected_close_at: str | None = None
    actual_close_at: str | None = None
    won: bool = False
    lost_reason: str | None = None
    created_by: int = 0
    assigned_to: int | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class FollowUp:
    """CRM Follow-up interaction."""
    follow_up_id: int
    lead_id: int
    task_id: int | None = None
    direction: str = "outbound"
    channel: str = "phone"
    summary: str = ""
    next_action: str | None = None
    next_action_at: str | None = None
    created_by: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


class CRMBackend:
    """CRM Backend for NADLANFIX."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create CRM tables if they don't exist."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS crm_leads (
                    lead_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT NOT NULL DEFAULT 'new',
                    source TEXT NOT NULL,
                    listing_id TEXT,
                    canonical_id TEXT,
                    city TEXT,
                    neighborhood TEXT,
                    price REAL,
                    rooms REAL,
                    area_sqm REAL,
                    contact_name TEXT,
                    phone TEXT,
                    email TEXT,
                    owner_id INTEGER,
                    assigned_to INTEGER,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    last_contact_at TEXT,
                    next_action_at TEXT,
                    tags TEXT,
                    metadata TEXT
                );

                CREATE TABLE IF NOT EXISTS crm_tasks (
                    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    priority TEXT NOT NULL DEFAULT 'medium',
                    lead_id INTEGER,
                    assigned_to INTEGER,
                    created_by INTEGER NOT NULL,
                    due_at TEXT,
                    completed_at TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    tags TEXT,
                    metadata TEXT,
                    FOREIGN KEY (lead_id) REFERENCES crm_leads(lead_id)
                );

                CREATE TABLE IF NOT EXISTS crm_notes (
                    note_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lead_id INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    tags TEXT,
                    metadata TEXT,
                    FOREIGN KEY (lead_id) REFERENCES crm_leads(lead_id)
                );

                CREATE TABLE IF NOT EXISTS crm_pipelines (
                    pipeline_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT,
                    stages TEXT NOT NULL,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS crm_activity_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    user_id INTEGER,
                    old_values TEXT,
                    new_values TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );

                CREATE TABLE IF NOT EXISTS crm_opportunities (
                    opportunity_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lead_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    value REAL,
                    probability REAL NOT NULL DEFAULT 0.0,
                    stage TEXT NOT NULL DEFAULT 'qualification',
                    expected_close_at TEXT,
                    actual_close_at TEXT,
                    won INTEGER NOT NULL DEFAULT 0,
                    lost_reason TEXT,
                    created_by INTEGER NOT NULL,
                    assigned_to INTEGER,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    tags TEXT,
                    metadata TEXT,
                    FOREIGN KEY (lead_id) REFERENCES crm_leads(lead_id)
                );

                CREATE TABLE IF NOT EXISTS crm_follow_ups (
                    follow_up_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lead_id INTEGER NOT NULL,
                    task_id INTEGER,
                    direction TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    next_action TEXT,
                    next_action_at TEXT,
                    created_by INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    tags TEXT,
                    metadata TEXT,
                    FOREIGN KEY (lead_id) REFERENCES crm_leads(lead_id),
                    FOREIGN KEY (task_id) REFERENCES crm_tasks(task_id)
                );

                CREATE INDEX IF NOT EXISTS idx_crm_leads_status ON crm_leads (status);
                CREATE INDEX IF NOT EXISTS idx_crm_leads_owner ON crm_leads (owner_id);
                CREATE INDEX IF NOT EXISTS idx_crm_leads_assigned ON crm_leads (assigned_to);
                CREATE INDEX IF NOT EXISTS idx_crm_tasks_lead ON crm_tasks (lead_id);
                CREATE INDEX IF NOT EXISTS idx_crm_tasks_assigned ON crm_tasks (assigned_to);
                CREATE INDEX IF NOT EXISTS idx_crm_notes_lead ON crm_notes (lead_id);
                CREATE INDEX IF NOT EXISTS idx_crm_activity_entity ON crm_activity_log (entity_type, entity_id);
                CREATE INDEX IF NOT EXISTS idx_crm_opportunities_lead ON crm_opportunities (lead_id);
                CREATE INDEX IF NOT EXISTS idx_crm_opportunities_stage ON crm_opportunities (stage);
                CREATE INDEX IF NOT EXISTS idx_crm_follow_ups_lead ON crm_follow_ups (lead_id);
            """)
            conn.commit()
        finally:
            conn.close()

    # -- Leads --

    def create_lead(self, lead: Lead) -> int:
        """Create a new lead."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.execute(
                """INSERT INTO crm_leads
                   (title, description, status, source, listing_id, canonical_id,
                    city, neighborhood, price, rooms, area_sqm, contact_name,
                    phone, email, owner_id, assigned_to, tags, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    lead.title, lead.description, lead.status.value, lead.source,
                    lead.listing_id, lead.canonical_id, lead.city, lead.neighborhood,
                    lead.price, lead.rooms, lead.area_sqm, lead.contact_name,
                    lead.phone, lead.email, lead.owner_id, lead.assigned_to,
                    json.dumps(lead.tags), json.dumps(lead.metadata),
                )
            )
            lead_id = cursor.lastrowid
            self._log_activity(conn, "lead", lead_id, "created", user_id=lead.owner_id,
                               new_values=json.dumps({"title": lead.title, "status": lead.status.value}))
            conn.commit()
            return lead_id
        finally:
            conn.close()

    def get_lead(self, lead_id: int) -> Optional[dict]:
        """Get a lead by ID."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM crm_leads WHERE lead_id = ?", (lead_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def update_lead(self, lead_id: int, updates: dict, user_id: int) -> bool:
        """Update a lead."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            existing = conn.execute("SELECT * FROM crm_leads WHERE lead_id = ?", (lead_id,)).fetchone()
            if not existing:
                return False

            # Build update query
            allowed_fields = {"title", "description", "status", "city", "neighborhood",
                              "price", "rooms", "area_sqm", "contact_name", "phone",
                              "email", "assigned_to", "next_action_at", "tags", "metadata"}
            set_clause = []
            params = []
            old_values = {}

            for field, value in updates.items():
                if field in allowed_fields:
                    set_clause.append(f"{field} = ?")
                    params.append(value)
                    old_values[field] = existing[field]

            if not set_clause:
                return False

            set_clause.append("updated_at = ?")
            params.append(datetime.now(timezone.utc).isoformat(timespec="seconds"))
            params.append(lead_id)

            conn.execute(f"UPDATE crm_leads SET {', '.join(set_clause)} WHERE lead_id = ?", params)
            self._log_activity(conn, "lead", lead_id, "updated", user_id=user_id,
                               old_values=json.dumps(old_values),
                               new_values=json.dumps(updates))
            conn.commit()
            return True
        finally:
            conn.close()

    def list_leads(self, status: str | None = None, assigned_to: int | None = None,
                   limit: int = 100, offset: int = 0) -> list[dict]:
        """List leads with optional filters."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            query = "SELECT * FROM crm_leads WHERE 1=1"
            params = []

            if status:
                query += " AND status = ?"
                params.append(status)
            if assigned_to:
                query += " AND assigned_to = ?"
                params.append(assigned_to)

            query += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def delete_lead(self, lead_id: int, user_id: int) -> bool:
        """Delete a lead."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.execute("DELETE FROM crm_leads WHERE lead_id = ?", (lead_id,))
            if cursor.rowcount > 0:
                self._log_activity(conn, "lead", lead_id, "deleted", user_id=user_id)
                conn.commit()
                return True
            return False
        finally:
            conn.close()

    # -- Tasks --

    def create_task(self, task: Task) -> int:
        """Create a new task."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.execute(
                """INSERT INTO crm_tasks
                   (title, description, status, priority, lead_id, assigned_to,
                    created_by, due_at, tags, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task.title, task.description, task.status.value, task.priority.value,
                    task.lead_id, task.assigned_to, task.created_by, task.due_at,
                    json.dumps(task.tags), json.dumps(task.metadata),
                )
            )
            task_id = cursor.lastrowid
            self._log_activity(conn, "task", task_id, "created", user_id=task.created_by,
                               new_values=json.dumps({"title": task.title, "status": task.status.value}))
            conn.commit()
            return task_id
        finally:
            conn.close()

    def get_task(self, task_id: int) -> Optional[dict]:
        """Get a task by ID."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM crm_tasks WHERE task_id = ?", (task_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def update_task(self, task_id: int, updates: dict, user_id: int) -> bool:
        """Update a task."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            existing = conn.execute("SELECT * FROM crm_tasks WHERE task_id = ?", (task_id,)).fetchone()
            if not existing:
                return False

            allowed_fields = {"title", "description", "status", "priority", "lead_id",
                              "assigned_to", "due_at", "tags", "metadata"}
            set_clause = []
            params = []
            old_values = {}

            for field, value in updates.items():
                if field in allowed_fields:
                    set_clause.append(f"{field} = ?")
                    params.append(value)
                    old_values[field] = existing[field]

            if not set_clause:
                return False

            set_clause.append("updated_at = ?")
            params.append(datetime.now(timezone.utc).isoformat(timespec="seconds"))
            params.append(task_id)

            conn.execute(f"UPDATE crm_tasks SET {', '.join(set_clause)} WHERE task_id = ?", params)
            self._log_activity(conn, "task", task_id, "updated", user_id=user_id,
                               old_values=json.dumps(old_values),
                               new_values=json.dumps(updates))
            conn.commit()
            return True
        finally:
            conn.close()

    def list_tasks(self, lead_id: int | None = None, assigned_to: int | None = None,
                   status: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        """List tasks with optional filters."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            query = "SELECT * FROM crm_tasks WHERE 1=1"
            params = []

            if lead_id:
                query += " AND lead_id = ?"
                params.append(lead_id)
            if assigned_to:
                query += " AND assigned_to = ?"
                params.append(assigned_to)
            if status:
                query += " AND status = ?"
                params.append(status)

            query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    # -- Notes --

    def create_note(self, note: Note) -> int:
        """Create a new note."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.execute(
                "INSERT INTO crm_notes (lead_id, content, created_by, tags, metadata) VALUES (?, ?, ?, ?, ?)",
                (note.lead_id, note.content, note.created_by, json.dumps(note.tags), json.dumps(note.metadata))
            )
            note_id = cursor.lastrowid
            self._log_activity(conn, "note", note_id, "created", user_id=note.created_by,
                               new_values=json.dumps({"lead_id": note.lead_id, "content": note.content[:100]}))
            conn.commit()
            return note_id
        finally:
            conn.close()

    def get_notes(self, lead_id: int, limit: int = 50, offset: int = 0) -> list[dict]:
        """Get notes for a lead."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM crm_notes WHERE lead_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (lead_id, limit, offset)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    # -- Pipelines --

    def create_pipeline(self, pipeline: Pipeline) -> int:
        """Create a new pipeline."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.execute(
                "INSERT INTO crm_pipelines (name, description, stages, is_default) VALUES (?, ?, ?, ?)",
                (pipeline.name, pipeline.description, json.dumps(pipeline.stages), 1 if pipeline.is_default else 0)
            )
            return cursor.lastrowid
        finally:
            conn.close()

    def get_pipelines(self) -> list[dict]:
        """Get all pipelines."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM crm_pipelines ORDER BY is_default DESC, name").fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    # -- Activity Log --

    def _log_activity(self, conn, entity_type: str, entity_id: int, action: str,
                      user_id: int | None = None, old_values: str | None = None,
                      new_values: str | None = None) -> None:
        """Log an activity."""
        conn.execute(
            "INSERT INTO crm_activity_log (entity_type, entity_id, action, user_id, old_values, new_values) VALUES (?, ?, ?, ?, ?, ?)",
            (entity_type, entity_id, action, user_id, old_values, new_values)
        )

    def get_activity_log(self, entity_type: str | None = None, entity_id: int | None = None,
                         limit: int = 100, offset: int = 0) -> list[dict]:
        """Get activity log entries."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            query = "SELECT * FROM crm_activity_log WHERE 1=1"
            params = []

            if entity_type:
                query += " AND entity_type = ?"
                params.append(entity_type)
            if entity_id:
                query += " AND entity_id = ?"
                params.append(entity_id)

            query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    # -- Stats --

    def get_stats(self) -> dict:
        """Get CRM statistics."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            leads_total = conn.execute("SELECT COUNT(*) FROM crm_leads").fetchone()[0]
            leads_by_status = conn.execute(
                "SELECT status, COUNT(*) FROM crm_leads GROUP BY status"
            ).fetchall()
            tasks_total = conn.execute("SELECT COUNT(*) FROM crm_tasks").fetchone()[0]
            tasks_pending = conn.execute(
                "SELECT COUNT(*) FROM crm_tasks WHERE status = 'pending'"
            ).fetchone()[0]
            notes_total = conn.execute("SELECT COUNT(*) FROM crm_notes").fetchone()[0]

            return {
                "leads_total": leads_total,
                "leads_by_status": {row[0]: row[1] for row in leads_by_status},
                "tasks_total": tasks_total,
                "tasks_pending": tasks_pending,
                "notes_total": notes_total,
                "opportunities_total": conn.execute("SELECT COUNT(*) FROM crm_opportunities").fetchone()[0],
                "opportunities_won": conn.execute("SELECT COUNT(*) FROM crm_opportunities WHERE won=1").fetchone()[0],
                "follow_ups_total": conn.execute("SELECT COUNT(*) FROM crm_follow_ups").fetchone()[0],
            }
        finally:
            conn.close()

    # -- Stage Transitions --

    def transition_lead_stage(self, lead_id: int, new_status: str, user_id: int, note: str | None = None) -> bool:
        """Transition a lead to a new stage with audit trail."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            existing = conn.execute("SELECT * FROM crm_leads WHERE lead_id = ?", (lead_id,)).fetchone()
            if not existing:
                return False

            old_status = existing["status"]
            if old_status == new_status:
                return True

            conn.execute(
                "UPDATE crm_leads SET status = ?, updated_at = ? WHERE lead_id = ?",
                (new_status, datetime.now(timezone.utc).isoformat(timespec="seconds"), lead_id),
            )
            self._log_activity(
                conn, "lead", lead_id, "stage_transition", user_id=user_id,
                old_values=json.dumps({"status": old_status}),
                new_values=json.dumps({"status": new_status, "note": note or ""}),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def assign_lead(self, lead_id: int, assigned_to: int | None, user_id: int) -> bool:
        """Assign or reassign a lead."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            existing = conn.execute("SELECT * FROM crm_leads WHERE lead_id = ?", (lead_id,)).fetchone()
            if not existing:
                return False

            old_assigned = existing["assigned_to"]
            if old_assigned == assigned_to:
                return True

            conn.execute(
                "UPDATE crm_leads SET assigned_to = ?, updated_at = ? WHERE lead_id = ?",
                (assigned_to, datetime.now(timezone.utc).isoformat(timespec="seconds"), lead_id),
            )
            self._log_activity(
                conn, "lead", lead_id, "assigned", user_id=user_id,
                old_values=json.dumps({"assigned_to": old_assigned}),
                new_values=json.dumps({"assigned_to": assigned_to}),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    # -- Follow-ups --

    def log_follow_up(self, follow_up: FollowUp) -> int:
        """Log a follow-up interaction."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.execute(
                """INSERT INTO crm_follow_ups
                   (lead_id, task_id, direction, channel, summary, next_action, next_action_at, created_by, tags, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    follow_up.lead_id, follow_up.task_id, follow_up.direction, follow_up.channel,
                    follow_up.summary, follow_up.next_action, follow_up.next_action_at,
                    follow_up.created_by, json.dumps(follow_up.tags), json.dumps(follow_up.metadata),
                ),
            )
            follow_up_id = cursor.lastrowid
            conn.execute(
                "UPDATE crm_leads SET last_contact_at = ?, next_action_at = ?, updated_at = ? WHERE lead_id = ?",
                (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    follow_up.next_action_at,
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    follow_up.lead_id,
                ),
            )
            self._log_activity(conn, "follow_up", follow_up_id, "created", user_id=follow_up.created_by,
                               new_values=json.dumps({"lead_id": follow_up.lead_id, "channel": follow_up.channel}))
            conn.commit()
            return follow_up_id
        finally:
            conn.close()

    def get_follow_ups(self, lead_id: int, limit: int = 50, offset: int = 0) -> list[dict]:
        """Get follow-ups for a lead."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM crm_follow_ups WHERE lead_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (lead_id, limit, offset),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    # -- Opportunities --

    def create_opportunity(self, opportunity: Opportunity) -> int:
        """Create a new opportunity."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            cursor = conn.execute(
                """INSERT INTO crm_opportunities
                   (lead_id, title, description, value, probability, stage, expected_close_at,
                    actual_close_at, won, lost_reason, created_by, assigned_to, tags, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    opportunity.lead_id, opportunity.title, opportunity.description, opportunity.value,
                    opportunity.probability, opportunity.stage, opportunity.expected_close_at,
                    opportunity.actual_close_at, 1 if opportunity.won else 0, opportunity.lost_reason,
                    opportunity.created_by, opportunity.assigned_to, json.dumps(opportunity.tags),
                    json.dumps(opportunity.metadata),
                ),
            )
            opportunity_id = cursor.lastrowid
            self._log_activity(conn, "opportunity", opportunity_id, "created", user_id=opportunity.created_by,
                               new_values=json.dumps({"title": opportunity.title, "stage": opportunity.stage}))
            conn.commit()
            return opportunity_id
        finally:
            conn.close()

    def get_opportunity(self, opportunity_id: int) -> dict | None:
        """Get an opportunity by ID."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM crm_opportunities WHERE opportunity_id = ?", (opportunity_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def update_opportunity(self, opportunity_id: int, updates: dict, user_id: int) -> bool:
        """Update an opportunity."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            existing = conn.execute("SELECT * FROM crm_opportunities WHERE opportunity_id = ?", (opportunity_id,)).fetchone()
            if not existing:
                return False

            allowed_fields = {"title", "description", "value", "probability", "stage",
                              "expected_close_at", "actual_close_at", "won", "lost_reason",
                              "assigned_to", "tags", "metadata"}
            set_clause = []
            params = []
            old_values = {}

            for field, value in updates.items():
                if field in allowed_fields:
                    set_clause.append(f"{field} = ?")
                    params.append(value)
                    old_values[field] = existing[field]

            if not set_clause:
                return False

            set_clause.append("updated_at = ?")
            params.append(datetime.now(timezone.utc).isoformat(timespec="seconds"))
            params.append(opportunity_id)

            conn.execute(f"UPDATE crm_opportunities SET {', '.join(set_clause)} WHERE opportunity_id = ?", params)
            self._log_activity(conn, "opportunity", opportunity_id, "updated", user_id=user_id,
                               old_values=json.dumps(old_values),
                               new_values=json.dumps(updates))
            conn.commit()
            return True
        finally:
            conn.close()

    def list_opportunities(self, lead_id: int | None = None, stage: str | None = None,
                           won: bool | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        """List opportunities with optional filters."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            query = "SELECT * FROM crm_opportunities WHERE 1=1"
            params = []

            if lead_id:
                query += " AND lead_id = ?"
                params.append(lead_id)
            if stage:
                query += " AND stage = ?"
                params.append(stage)
            if won is not None:
                query += " AND won = ?"
                params.append(1 if won else 0)

            query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()
