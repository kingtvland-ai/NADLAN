"""CRM Leads module.

Provides lead management business logic and API endpoints.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from app.crm.crm_backend import CRMBackend, Lead, LeadStatus


class LeadSource(str, Enum):
    YAD2 = "yad2"
    FACEBOOK = "facebook"
    ONMAP = "onmap"
    MANUAL = "manual"
    IMPORT = "import"


@dataclass
class LeadCreate:
    """Lead creation payload."""
    title: str
    description: str = ""
    source: str = LeadSource.MANUAL.value
    listing_id: str | None = None
    canonical_id: str | None = None
    city: str | None = None
    neighborhood: str | None = None
    price: float | None = None
    rooms: float | None = None
    area_sqm: float | None = None
    contact_name: str | None = None
    phone: str | None = None
    email: str | None = None
    owner_id: int | None = None
    assigned_to: int | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class LeadUpdate:
    """Lead update payload."""
    title: str | None = None
    description: str | None = None
    status: str | None = None
    city: str | None = None
    neighborhood: str | None = None
    price: float | None = None
    rooms: float | None = None
    area_sqm: float | None = None
    contact_name: str | None = None
    phone: str | None = None
    email: str | None = None
    assigned_to: int | None = None
    next_action_at: str | None = None
    tags: list[str] | None = None
    metadata: dict | None = None


class LeadService:
    """Lead business logic service."""

    def __init__(self, db_path):
        self.crm = CRMBackend(db_path)

    def create(self, payload: LeadCreate, user_id: int) -> dict:
        """Create a new lead."""
        lead = Lead(
            lead_id=0,
            title=payload.title,
            description=payload.description,
            status=LeadStatus.NEW,
            source=payload.source,
            listing_id=payload.listing_id,
            canonical_id=payload.canonical_id,
            city=payload.city,
            neighborhood=payload.neighborhood,
            price=payload.price,
            rooms=payload.rooms,
            area_sqm=payload.area_sqm,
            contact_name=payload.contact_name,
            phone=payload.phone,
            email=payload.email,
            owner_id=payload.owner_id or user_id,
            assigned_to=payload.assigned_to,
            tags=payload.tags,
            metadata=payload.metadata,
        )
        lead_id = self.crm.create_lead(lead)
        return {"lead_id": lead_id, "status": "created"}

    def get(self, lead_id: int) -> dict | None:
        """Get a lead by ID."""
        return self.crm.get_lead(lead_id)

    def update(self, lead_id: int, payload: LeadUpdate, user_id: int) -> bool:
        """Update a lead."""
        updates = {k: v for k, v in payload.__dict__.items() if v is not None}
        if not updates:
            return False
        return self.crm.update_lead(lead_id, updates, user_id)

    def delete(self, lead_id: int, user_id: int) -> bool:
        """Delete a lead."""
        return self.crm.delete_lead(lead_id, user_id)

    def list(self, status: str | None = None, assigned_to: int | None = None,
             city: str | None = None, source: str | None = None,
             limit: int = 100, offset: int = 0) -> list[dict]:
        """List leads with filters."""
        leads = self.crm.list_leads(status=status, assigned_to=assigned_to,
                                    limit=limit, offset=offset)
        if city:
            leads = [l for l in leads if l.get("city") == city]
        if source:
            leads = [l for l in leads if l.get("source") == source]
        return leads

    def transition_stage(self, lead_id: int, new_status: str, user_id: int,
                         note: str | None = None) -> bool:
        """Transition a lead to a new stage."""
        return self.crm.transition_lead_stage(lead_id, new_status, user_id, note)

    def assign(self, lead_id: int, assigned_to: int | None, user_id: int) -> bool:
        """Assign a lead to a user."""
        return self.crm.assign_lead(lead_id, assigned_to, user_id)

    def search(self, query: str, limit: int = 50) -> list[dict]:
        """Search leads by title, contact name, or city."""
        leads = self.crm.list_leads(limit=1000)
        q = query.lower()
        results = []
        for lead in leads:
            text = " ".join([
                lead.get("title", ""),
                lead.get("contact_name", ""),
                lead.get("city", ""),
                lead.get("neighborhood", ""),
            ]).lower()
            if q in text:
                results.append(lead)
                if len(results) >= limit:
                    break
        return results
