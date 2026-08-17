"""CRM Pipeline module.

Provides pipeline management business logic and API endpoints.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.crm.crm_backend import CRMBackend, Pipeline


@dataclass
class StageDefinition:
    """Pipeline stage definition."""
    name: str
    order: int
    color: str = "#gray"
    is_won: bool = False
    is_lost: bool = False


@dataclass
class PipelineCreate:
    """Pipeline creation payload."""
    name: str
    description: str = ""
    stages: list[dict] = field(default_factory=list)
    is_default: bool = False


class PipelineService:
    """Pipeline business logic service."""

    DEFAULT_STAGES = [
        {"name": "new", "order": 0, "color": "#3b82f6"},
        {"name": "contacted", "order": 1, "color": "#eab308"},
        {"name": "qualified", "order": 2, "color": "#f97316"},
        {"name": "proposal", "order": 3, "color": "#a855f7"},
        {"name": "negotiation", "order": 4, "color": "#6366f1"},
        {"name": "won", "order": 5, "color": "#22c55e", "is_won": True},
        {"name": "lost", "order": 6, "color": "#ef4444", "is_lost": True},
    ]

    def __init__(self, db_path):
        self.crm = CRMBackend(db_path)
        self._ensure_default_pipeline()

    def _ensure_default_pipeline(self) -> None:
        """Ensure a default pipeline exists."""
        pipelines = self.crm.get_pipelines()
        if not pipelines:
            pipeline = Pipeline(
                pipeline_id=0,
                name="Default Sales Pipeline",
                description="Standard lead-to-sale pipeline",
                stages=self.DEFAULT_STAGES,
                is_default=True,
            )
            self.crm.create_pipeline(pipeline)

    def create(self, payload: PipelineCreate) -> dict:
        """Create a new pipeline."""
        pipeline = Pipeline(
            pipeline_id=0,
            name=payload.name,
            description=payload.description,
            stages=payload.stages,
            is_default=payload.is_default,
        )
        pipeline_id = self.crm.create_pipeline(pipeline)
        return {"pipeline_id": pipeline_id, "status": "created"}

    def get(self, pipeline_id: int) -> dict | None:
        """Get a pipeline by ID."""
        pipelines = self.crm.get_pipelines()
        for p in pipelines:
            if p.get("pipeline_id") == pipeline_id:
                return p
        return None

    def list(self) -> list[dict]:
        """List all pipelines."""
        return self.crm.get_pipelines()

    def get_default(self) -> dict | None:
        """Get the default pipeline."""
        pipelines = self.crm.get_pipelines()
        for p in pipelines:
            if p.get("is_default"):
                return p
        return pipelines[0] if pipelines else None

    def get_stages(self, pipeline_id: int | None = None) -> list[dict]:
        """Get stages for a pipeline."""
        pipeline = self.get(pipeline_id) if pipeline_id else self.get_default()
        if not pipeline:
            return self.DEFAULT_STAGES
        return json.loads(pipeline.get("stages", "[]")) if isinstance(pipeline.get("stages"), str) else pipeline.get("stages", [])

    def advance_stage(self, lead_id: int, user_id: int) -> dict | None:
        """Advance a lead to the next stage."""
        lead = self.crm.get_lead(lead_id)
        if not lead:
            return None

        stages = self.get_stages()
        current_order = None
        for stage in stages:
            if stage["name"] == lead.get("status"):
                current_order = stage["order"]
                break

        if current_order is None:
            next_stage = stages[0] if stages else None
        else:
            next_stage = None
            for stage in stages:
                if stage["order"] > current_order:
                    next_stage = stage
                    break

        if not next_stage:
            return {"error": "already at final stage"}

        self.crm.transition_lead_stage(lead_id, next_stage["name"], user_id)
        return {"lead_id": lead_id, "new_stage": next_stage["name"]}
