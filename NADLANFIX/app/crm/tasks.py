"""CRM Tasks module.

Provides task management business logic and API endpoints.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from app.crm.crm_backend import CRMBackend, Task, TaskPriority, TaskStatus


@dataclass
class TaskCreate:
    """Task creation payload."""
    title: str
    description: str = ""
    status: str = TaskStatus.PENDING.value
    priority: str = TaskPriority.MEDIUM.value
    lead_id: int | None = None
    assigned_to: int | None = None
    due_at: str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class TaskUpdate:
    """Task update payload."""
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    lead_id: int | None = None
    assigned_to: int | None = None
    due_at: str | None = None
    completed_at: str | None = None
    tags: list[str] | None = None
    metadata: dict | None = None


class TaskService:
    """Task business logic service."""

    def __init__(self, db_path):
        self.crm = CRMBackend(db_path)

    def create(self, payload: TaskCreate, user_id: int) -> dict:
        """Create a new task."""
        task = Task(
            task_id=0,
            title=payload.title,
            description=payload.description,
            status=TaskStatus(payload.status),
            priority=TaskPriority(payload.priority),
            lead_id=payload.lead_id,
            assigned_to=payload.assigned_to,
            created_by=user_id,
            due_at=payload.due_at,
            tags=payload.tags,
            metadata=payload.metadata,
        )
        task_id = self.crm.create_task(task)
        return {"task_id": task_id, "status": "created"}

    def get(self, task_id: int) -> dict | None:
        """Get a task by ID."""
        return self.crm.get_task(task_id)

    def update(self, task_id: int, payload: TaskUpdate, user_id: int) -> bool:
        """Update a task."""
        updates = {k: v for k, v in payload.__dict__.items() if v is not None}
        if not updates:
            return False
        return self.crm.update_task(task_id, updates, user_id)

    def list(self, lead_id: int | None = None, assigned_to: int | None = None,
             status: str | None = None, limit: int = 100, offset: int = 0) -> list[dict]:
        """List tasks with filters."""
        return self.crm.list_tasks(lead_id=lead_id, assigned_to=assigned_to,
                                   status=status, limit=limit, offset=offset)

    def complete(self, task_id: int, user_id: int) -> bool:
        """Mark a task as completed."""
        return self.crm.update_task(task_id, {
            "status": TaskStatus.COMPLETED.value,
            "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, user_id)

    def cancel(self, task_id: int, user_id: int) -> bool:
        """Cancel a task."""
        return self.crm.update_task(task_id, {
            "status": TaskStatus.CANCELLED.value,
        }, user_id)
