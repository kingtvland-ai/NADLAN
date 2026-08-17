"""Role-Based Access Control (RBAC) for NADLANFIX.

Defines roles, permissions, and access control logic for the three main layers:
- User-facing application
- Admin dashboard
- CRM backend
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Role(Enum):
    """User roles in the system."""
    USER = "user"
    ADMIN = "admin"
    AGENT = "agent"
    MANAGER = "manager"
    SYSTEM = "system"


class Permission(Enum):
    """Granular permissions."""
    # User permissions
    VIEW_LISTINGS = "view_listings"
    SEARCH_LISTINGS = "search_listings"
    VIEW_OPPORTUNITIES = "view_opportunities"
    VIEW_MAP = "view_map"
    VIEW_PLANS = "view_plans"

    # Admin permissions
    MANAGE_SOURCES = "manage_sources"
    MANAGE_USERS = "manage_users"
    VIEW_HEALTH = "view_health"
    MANAGE_SETTINGS = "manage_settings"
    VIEW_AUDIT = "view_audit"

    # CRM permissions
    VIEW_LEADS = "view_leads"
    CREATE_LEADS = "create_leads"
    EDIT_LEADS = "edit_leads"
    DELETE_LEADS = "delete_leads"
    VIEW_PIPELINES = "view_pipelines"
    MANAGE_PIPELINES = "manage_pipelines"
    VIEW_TASKS = "view_tasks"
    CREATE_TASKS = "create_tasks"
    EDIT_TASKS = "edit_tasks"
    DELETE_TASKS = "delete_tasks"

    # System permissions
    RUN_INGESTION = "run_ingestion"
    VIEW_LOGS = "view_logs"
    MANAGE_BACKUP = "manage_backup"


# Role-Permission mapping
ROLE_PERMISSIONS: dict[Role, list[Permission]] = {
    Role.USER: [
        Permission.VIEW_LISTINGS,
        Permission.SEARCH_LISTINGS,
        Permission.VIEW_OPPORTUNITIES,
        Permission.VIEW_MAP,
        Permission.VIEW_PLANS,
    ],
    Role.AGENT: [
        Permission.VIEW_LISTINGS,
        Permission.SEARCH_LISTINGS,
        Permission.VIEW_OPPORTUNITIES,
        Permission.VIEW_MAP,
        Permission.VIEW_PLANS,
        Permission.VIEW_LEADS,
        Permission.CREATE_LEADS,
        Permission.EDIT_LEADS,
        Permission.VIEW_TASKS,
        Permission.CREATE_TASKS,
        Permission.EDIT_TASKS,
    ],
    Role.MANAGER: [
        Permission.VIEW_LISTINGS,
        Permission.SEARCH_LISTINGS,
        Permission.VIEW_OPPORTUNITIES,
        Permission.VIEW_MAP,
        Permission.VIEW_PLANS,
        Permission.VIEW_LEADS,
        Permission.CREATE_LEADS,
        Permission.EDIT_LEADS,
        Permission.DELETE_LEADS,
        Permission.VIEW_PIPELINES,
        Permission.MANAGE_PIPELINES,
        Permission.VIEW_TASKS,
        Permission.CREATE_TASKS,
        Permission.EDIT_TASKS,
        Permission.DELETE_TASKS,
        Permission.VIEW_HEALTH,
    ],
    Role.ADMIN: [
        Permission.VIEW_LISTINGS,
        Permission.SEARCH_LISTINGS,
        Permission.VIEW_OPPORTUNITIES,
        Permission.VIEW_MAP,
        Permission.VIEW_PLANS,
        Permission.MANAGE_SOURCES,
        Permission.MANAGE_USERS,
        Permission.VIEW_HEALTH,
        Permission.MANAGE_SETTINGS,
        Permission.VIEW_AUDIT,
        Permission.VIEW_LEADS,
        Permission.CREATE_LEADS,
        Permission.EDIT_LEADS,
        Permission.DELETE_LEADS,
        Permission.VIEW_PIPELINES,
        Permission.MANAGE_PIPELINES,
        Permission.VIEW_TASKS,
        Permission.CREATE_TASKS,
        Permission.EDIT_TASKS,
        Permission.DELETE_TASKS,
        Permission.RUN_INGESTION,
        Permission.VIEW_LOGS,
        Permission.MANAGE_BACKUP,
    ],
    Role.SYSTEM: list(Permission),  # System has all permissions
}


@dataclass
class User:
    """User with role and permissions."""
    user_id: int
    username: str
    role: Role
    permissions: list[Permission] = field(default_factory=list)
    is_active: bool = True

    def has_permission(self, permission: Permission) -> bool:
        """Check if user has a specific permission."""
        if self.role == Role.SYSTEM:
            return True
        if permission in self.permissions:
            return True
        return permission in ROLE_PERMISSIONS.get(self.role, [])

    def has_any_permission(self, permissions: list[Permission]) -> bool:
        """Check if user has any of the specified permissions."""
        return any(self.has_permission(p) for p in permissions)

    def has_all_permissions(self, permissions: list[Permission]) -> bool:
        """Check if user has all of the specified permissions."""
        return all(self.has_permission(p) for p in permissions)


class RBAC:
    """Role-Based Access Control manager."""

    def __init__(self):
        self._users: dict[int, User] = {}
        self._role_hierarchy = {
            Role.SYSTEM: 5,
            Role.ADMIN: 4,
            Role.MANAGER: 3,
            Role.AGENT: 2,
            Role.USER: 1,
        }

    def register_user(self, user: User) -> None:
        """Register a user in the RBAC system."""
        self._users[user.user_id] = user

    def get_user(self, user_id: int) -> Optional[User]:
        """Get a user by ID."""
        return self._users.get(user_id)

    def check_permission(self, user_id: int, permission: Permission) -> bool:
        """Check if a user has a specific permission."""
        user = self.get_user(user_id)
        if not user or not user.is_active:
            return False
        return user.has_permission(permission)

    def check_any_permission(self, user_id: int, permissions: list[Permission]) -> bool:
        """Check if a user has any of the specified permissions."""
        user = self.get_user(user_id)
        if not user or not user.is_active:
            return False
        return user.has_any_permission(permissions)

    def get_user_permissions(self, user_id: int) -> list[Permission]:
        """Get all permissions for a user."""
        user = self.get_user(user_id)
        if not user or not user.is_active:
            return []
        return list(set(user.permissions + ROLE_PERMISSIONS.get(user.role, [])))

    def get_role_hierarchy(self) -> dict[Role, int]:
        """Get the role hierarchy (higher number = more permissions)."""
        return dict(self._role_hierarchy)

    def can_access(self, user_id: int, layer: str) -> bool:
        """Check if a user can access a specific layer."""
        user = self.get_user(user_id)
        if not user or not user.is_active:
            return False

        if layer == "user":
            return True  # All active users can access user layer
        elif layer == "admin":
            return user.role in (Role.ADMIN, Role.SYSTEM)
        elif layer == "crm":
            return user.role in (Role.AGENT, Role.MANAGER, Role.ADMIN, Role.SYSTEM)
        return False


ENDPOINT_PERMISSIONS: dict[str, list[Permission]] = {
    # User endpoints
    "/api/listings": [Permission.VIEW_LISTINGS],
    "/api/search": [Permission.SEARCH_LISTINGS],
    "/api/opportunities": [Permission.VIEW_OPPORTUNITIES],
    "/api/map": [Permission.VIEW_MAP],
    "/api/plans": [Permission.VIEW_PLANS],
    # Admin endpoints
    "/api/admin/sources": [Permission.MANAGE_SOURCES],
    "/api/admin/users": [Permission.MANAGE_USERS],
    "/api/admin/health": [Permission.VIEW_HEALTH],
    "/api/admin/settings": [Permission.MANAGE_SETTINGS],
    "/api/admin/audit": [Permission.VIEW_AUDIT],
    # CRM endpoints
    "/api/crm/leads": [Permission.VIEW_LEADS],
    "/api/crm/leads/create": [Permission.CREATE_LEADS],
    "/api/crm/leads/update": [Permission.EDIT_LEADS],
    "/api/crm/leads/delete": [Permission.DELETE_LEADS],
    "/api/crm/pipelines": [Permission.VIEW_PIPELINES],
    "/api/crm/pipelines/manage": [Permission.MANAGE_PIPELINES],
    "/api/crm/tasks": [Permission.VIEW_TASKS],
    "/api/crm/tasks/create": [Permission.CREATE_TASKS],
    "/api/crm/tasks/update": [Permission.EDIT_TASKS],
    "/api/crm/tasks/delete": [Permission.DELETE_TASKS],
    # System endpoints
    "/api/admin/run": [Permission.RUN_INGESTION],
    "/api/admin/logs": [Permission.VIEW_LOGS],
    "/api/admin/backup": [Permission.MANAGE_BACKUP],
}


class EndpointGuard:
    """Endpoint access control helper."""

    def __init__(self, rbac: RBAC):
        self.rbac = rbac

    def check(self, user_id: int, path: str, method: str = "GET") -> bool:
        """Check if a user can access an endpoint.

        Args:
            user_id: User ID
            path: Request path
            method: HTTP method

        Returns:
            True if access is allowed
        """
        if not path:
            return False

        normalized = path.rstrip("/").lower()
        if method == "POST" and not normalized.endswith("create"):
            normalized += "/create"
        elif method in ("PUT", "PATCH") and not normalized.endswith("update"):
            normalized += "/update"
        elif method == "DELETE" and not normalized.endswith("delete"):
            normalized += "/delete"

        required = ENDPOINT_PERMISSIONS.get(normalized)
        if not required:
            return True

        return self.rbac.check_any_permission(user_id, required)

    def required_permissions(self, path: str, method: str = "GET") -> list[Permission]:
        """Get required permissions for an endpoint."""
        normalized = path.rstrip("/").lower()
        if method == "POST" and not normalized.endswith("create"):
            normalized += "/create"
        elif method in ("PUT", "PATCH") and not normalized.endswith("update"):
            normalized += "/update"
        elif method == "DELETE" and not normalized.endswith("delete"):
            normalized += "/delete"
        return ENDPOINT_PERMISSIONS.get(normalized, [])
