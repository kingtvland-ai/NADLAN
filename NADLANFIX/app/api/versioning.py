"""API Versioning for NADLANFIX.

Provides API versioning support with v1/v2 separation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class APIVersion:
    """API version definition."""
    version: str
    path_prefix: str
    description: str
    is_deprecated: bool = False
    deprecated_at: str | None = None


class APIVersioning:
    """API versioning manager."""

    def __init__(self):
        self._versions: dict[str, APIVersion] = {}
        self._routes: dict[str, dict[str, Callable]] = {}
        self._register_default_versions()

    def _register_default_versions(self) -> None:
        """Register default API versions."""
        self.register_version(APIVersion(
            version="v1",
            path_prefix="/api/v1",
            description="Initial API version",
            is_deprecated=False,
        ))
        self.register_version(APIVersion(
            version="v2",
            path_prefix="/api/v2",
            description="Current API version with enhanced features",
            is_deprecated=False,
        ))

    def register_version(self, version: APIVersion) -> None:
        """Register an API version."""
        self._versions[version.version] = version
        self._routes[version.version] = {}

    def register_route(self, version: str, path: str, handler: Callable) -> None:
        """Register a route for a specific API version."""
        if version not in self._routes:
            raise ValueError(f"Unknown API version: {version}")
        self._routes[version][path] = handler

    def get_route(self, version: str, path: str) -> Optional[Callable]:
        """Get a route handler for a specific version and path."""
        if version not in self._routes:
            return None
        return self._routes[version].get(path)

    def get_version_from_path(self, path: str) -> tuple[str, str]:
        """Extract version and remaining path from request path.

        Returns:
            Tuple of (version, remaining_path)
        """
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "api" and parts[1] in self._versions:
            return parts[1], "/" + "/".join(parts[2:])
        return "v1", path  # Default to v1

    def get_all_versions(self) -> list[APIVersion]:
        """Get all registered API versions."""
        return list(self._versions.values())

    def is_deprecated(self, version: str) -> bool:
        """Check if a version is deprecated."""
        if version not in self._versions:
            return False
        return self._versions[version].is_deprecated
