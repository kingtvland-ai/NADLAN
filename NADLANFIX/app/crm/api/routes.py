"""CRM API routes.

Provides HTTP endpoint handlers for CRM operations.
"""

from __future__ import annotations

import json
from typing import Any

from app.auth.rbac import Permission
from app.crm.crm_backend import CRMBackend
from app.crm.leads import LeadService, LeadUpdate, LeadCreate
from app.crm.pipeline import PipelineService
from app.crm.tasks import TaskService, TaskCreate, TaskUpdate
from app.crm.notes import NoteService, NoteCreate
from app.crm.owners import OwnerService


class CRMAPI:
    """CRM API endpoint handlers."""

    def __init__(self, db_path, rbac=None):
        self.leads = LeadService(db_path)
        self.pipelines = PipelineService(db_path)
        self.tasks = TaskService(db_path)
        self.notes = NoteService(db_path)
        self.owners = OwnerService(db_path)
        self.rbac = rbac

    def _check_permission(self, user_id: int, permission: Permission) -> bool:
        """Check if user has permission."""
        if not self.rbac:
            return True
        return self.rbac.check_permission(user_id, permission)

    # -- Leads --

    def list_leads(self, q: dict, user_id: int) -> dict:
        """List leads with filters."""
        if not self._check_permission(user_id, Permission.VIEW_LEADS):
            return {"error": "forbidden"}, 403

        status = q.get("status")
        assigned_to = int(q["assigned_to"]) if q.get("assigned_to") else None
        city = q.get("city")
        source = q.get("source")
        limit = int(q.get("limit", 100))
        offset = int(q.get("offset", 0))

        leads = self.leads.list(
            status=status, assigned_to=assigned_to, city=city, source=source,
            limit=limit, offset=offset,
        )
        return {"leads": leads, "count": len(leads)}

    def get_lead(self, lead_id: int, user_id: int) -> dict:
        """Get a lead by ID."""
        if not self._check_permission(user_id, Permission.VIEW_LEADS):
            return {"error": "forbidden"}, 403

        lead = self.leads.get(lead_id)
        if not lead:
            return {"error": "not found"}, 404
        return lead

    def create_lead(self, payload: dict, user_id: int) -> dict:
        """Create a new lead."""
        if not self._check_permission(user_id, Permission.CREATE_LEADS):
            return {"error": "forbidden"}, 403

        lead_create = LeadCreate(**payload)
        result = self.leads.create(lead_create, user_id)
        return result, 201

    def update_lead(self, lead_id: int, payload: dict, user_id: int) -> dict:
        """Update a lead."""
        if not self._check_permission(user_id, Permission.EDIT_LEADS):
            return {"error": "forbidden"}, 403

        lead_update = LeadUpdate(**payload)
        success = self.leads.update(lead_id, lead_update, user_id)
        if not success:
            return {"error": "update failed"}, 400
        return {"lead_id": lead_id, "status": "updated"}

    def delete_lead(self, lead_id: int, user_id: int) -> dict:
        """Delete a lead."""
        if not self._check_permission(user_id, Permission.DELETE_LEADS):
            return {"error": "forbidden"}, 403

        success = self.leads.delete(lead_id, user_id)
        if not success:
            return {"error": "not found"}, 404
        return {"lead_id": lead_id, "status": "deleted"}

    def transition_lead(self, lead_id: int, new_status: str, user_id: int, note: str | None = None) -> dict:
        """Transition a lead stage."""
        if not self._check_permission(user_id, Permission.EDIT_LEADS):
            return {"error": "forbidden"}, 403

        success = self.leads.transition_stage(lead_id, new_status, user_id, note)
        if not success:
            return {"error": "transition failed"}, 400
        return {"lead_id": lead_id, "new_status": new_status}

    def assign_lead(self, lead_id: int, assigned_to: int | None, user_id: int) -> dict:
        """Assign a lead."""
        if not self._check_permission(user_id, Permission.EDIT_LEADS):
            return {"error": "forbidden"}, 403

        success = self.leads.assign(lead_id, assigned_to, user_id)
        if not success:
            return {"error": "assignment failed"}, 400
        return {"lead_id": lead_id, "assigned_to": assigned_to}

    def search_leads(self, query: str, user_id: int, limit: int = 50) -> dict:
        """Search leads."""
        if not self._check_permission(user_id, Permission.VIEW_LEADS):
            return {"error": "forbidden"}, 403

        results = self.leads.search(query, limit=limit)
        return {"results": results, "count": len(results)}

    # -- Pipelines --

    def list_pipelines(self, user_id: int) -> dict:
        """List all pipelines."""
        if not self._check_permission(user_id, Permission.VIEW_PIPELINES):
            return {"error": "forbidden"}, 403

        pipelines = self.pipelines.list()
        return {"pipelines": pipelines}

    def get_pipeline(self, pipeline_id: int, user_id: int) -> dict:
        """Get a pipeline by ID."""
        if not self._check_permission(user_id, Permission.VIEW_PIPELINES):
            return {"error": "forbidden"}, 403

        pipeline = self.pipelines.get(pipeline_id)
        if not pipeline:
            return {"error": "not found"}, 404
        return pipeline

    def create_pipeline(self, payload: dict, user_id: int) -> dict:
        """Create a new pipeline."""
        if not self._check_permission(user_id, Permission.MANAGE_PIPELINES):
            return {"error": "forbidden"}, 403

        from app.crm.pipeline import PipelineCreate
        pipeline_create = PipelineCreate(**payload)
        result = self.pipelines.create(pipeline_create)
        return result, 201

    def advance_lead_stage(self, lead_id: int, user_id: int) -> dict:
        """Advance a lead to the next stage."""
        if not self._check_permission(user_id, Permission.EDIT_LEADS):
            return {"error": "forbidden"}, 403

        result = self.pipelines.advance_stage(lead_id, user_id)
        if not result:
            return {"error": "not found"}, 404
        return result

    # -- Tasks --

    def list_tasks(self, q: dict, user_id: int) -> dict:
        """List tasks with filters."""
        if not self._check_permission(user_id, Permission.VIEW_TASKS):
            return {"error": "forbidden"}, 403

        lead_id = int(q["lead_id"]) if q.get("lead_id") else None
        assigned_to = int(q["assigned_to"]) if q.get("assigned_to") else None
        status = q.get("status")
        limit = int(q.get("limit", 100))
        offset = int(q.get("offset", 0))

        tasks = self.tasks.list(lead_id=lead_id, assigned_to=assigned_to,
                                status=status, limit=limit, offset=offset)
        return {"tasks": tasks, "count": len(tasks)}

    def create_task(self, payload: dict, user_id: int) -> dict:
        """Create a new task."""
        if not self._check_permission(user_id, Permission.CREATE_TASKS):
            return {"error": "forbidden"}, 403

        task_create = TaskCreate(**payload)
        result = self.tasks.create(task_create, user_id)
        return result, 201

    def update_task(self, task_id: int, payload: dict, user_id: int) -> dict:
        """Update a task."""
        if not self._check_permission(user_id, Permission.EDIT_TASKS):
            return {"error": "forbidden"}, 403

        task_update = TaskUpdate(**payload)
        success = self.tasks.update(task_id, task_update, user_id)
        if not success:
            return {"error": "update failed"}, 400
        return {"task_id": task_id, "status": "updated"}

    def complete_task(self, task_id: int, user_id: int) -> dict:
        """Complete a task."""
        if not self._check_permission(user_id, Permission.EDIT_TASKS):
            return {"error": "forbidden"}, 403

        success = self.tasks.complete(task_id, user_id)
        if not success:
            return {"error": "not found"}, 404
        return {"task_id": task_id, "status": "completed"}

    def cancel_task(self, task_id: int, user_id: int) -> dict:
        """Cancel a task."""
        if not self._check_permission(user_id, Permission.EDIT_TASKS):
            return {"error": "forbidden"}, 403

        success = self.tasks.cancel(task_id, user_id)
        if not success:
            return {"error": "not found"}, 404
        return {"task_id": task_id, "status": "cancelled"}

    # -- Notes --

    def list_notes(self, lead_id: int, user_id: int, limit: int = 50, offset: int = 0) -> dict:
        """List notes for a lead."""
        if not self._check_permission(user_id, Permission.VIEW_LEADS):
            return {"error": "forbidden"}, 403

        notes = self.notes.list_for_lead(lead_id, limit=limit, offset=offset)
        return {"notes": notes, "count": len(notes)}

    def create_note(self, payload: dict, user_id: int) -> dict:
        """Create a new note."""
        if not self._check_permission(user_id, Permission.EDIT_LEADS):
            return {"error": "forbidden"}, 403

        note_create = NoteCreate(**payload)
        result = self.notes.create(note_create, user_id)
        return result, 201

    def delete_note(self, note_id: int, user_id: int) -> dict:
        """Delete a note."""
        if not self._check_permission(user_id, Permission.EDIT_LEADS):
            return {"error": "forbidden"}, 403

        success = self.notes.delete(note_id, user_id)
        if not success:
            return {"error": "not found"}, 404
        return {"note_id": note_id, "status": "deleted"}

    # -- Stats --

    def get_stats(self, user_id: int) -> dict:
        """Get CRM statistics."""
        if not self._check_permission(user_id, Permission.VIEW_LEADS):
            return {"error": "forbidden"}, 403

        stats = self.leads.crm.get_stats()
        return stats
