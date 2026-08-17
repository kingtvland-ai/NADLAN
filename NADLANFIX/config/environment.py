"""Multi-environment configuration for NADLANFIX.

Supports development, staging, and production environments with appropriate
separation, secrets management, and deployment policies.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class Environment(Enum):
    """Deployment environments."""
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


@dataclass
class EnvironmentConfig:
    """Configuration for a specific environment."""
    name: str
    database_url: str
    firestore_project: str | None = None
    log_level: str = "INFO"
    debug: bool = False
    auth_required: bool = False
    basic_auth: str | None = None
    telegram_token: str | None = None
    whatsapp_auth_dir: Path | None = None
    scheduler_enabled: bool = True
    auto_cycle_hours: int = 12
    max_workers: int = 4
    retention_days: int = 30
    backup_enabled: bool = True
    monitoring_enabled: bool = True
    extra: dict = field(default_factory=dict)


class EnvironmentManager:
    """Manages multi-environment configuration."""

    def __init__(self, root_dir: Path | str | None = None):
        self.root_dir = Path(root_dir) if root_dir else Path(__file__).parent.parent.parent
        self._configs: dict[str, EnvironmentConfig] = {}
        self._current_env: Environment | None = None
        self._load_configs()

    def _load_configs(self) -> None:
        """Load environment configurations."""
        # Default configurations
        self._configs = {
            Environment.DEVELOPMENT.value: EnvironmentConfig(
                name=Environment.DEVELOPMENT.value,
                database_url=str(self.root_dir / "data" / "planwatch_dev.sqlite3"),
                firestore_project=None,
                log_level="DEBUG",
                debug=True,
                auth_required=False,
                basic_auth=None,
                scheduler_enabled=True,
                auto_cycle_hours=24,
                max_workers=2,
                retention_days=7,
                backup_enabled=False,
                monitoring_enabled=False,
            ),
            Environment.STAGING.value: EnvironmentConfig(
                name=Environment.STAGING.value,
                database_url=str(self.root_dir / "data" / "planwatch_staging.sqlite3"),
                firestore_project="nadlanfix-staging",
                log_level="INFO",
                debug=False,
                auth_required=True,
                basic_auth=os.environ.get("PLANWATCH_BASIC_AUTH"),
                telegram_token=os.environ.get("PLANWATCH_TELEGRAM_TOKEN"),
                whatsapp_auth_dir=Path(os.environ.get("WHATSAPP_AUTH_DIR", str(self.root_dir / "data" / "whatsapp-auth"))),
                scheduler_enabled=True,
                auto_cycle_hours=12,
                max_workers=4,
                retention_days=14,
                backup_enabled=True,
                monitoring_enabled=True,
            ),
            Environment.PRODUCTION.value: EnvironmentConfig(
                name=Environment.PRODUCTION.value,
                database_url=str(self.root_dir / "data" / "planwatch.sqlite3"),
                firestore_project="nadlanfix-prod",
                log_level="WARNING",
                debug=False,
                auth_required=True,
                basic_auth=os.environ.get("PLANWATCH_BASIC_AUTH"),
                telegram_token=os.environ.get("PLANWATCH_TELEGRAM_TOKEN"),
                whatsapp_auth_dir=Path(os.environ.get("WHATSAPP_AUTH_DIR", str(self.root_dir / "data" / "whatsapp-auth"))),
                scheduler_enabled=True,
                auto_cycle_hours=12,
                max_workers=8,
                retention_days=30,
                backup_enabled=True,
                monitoring_enabled=True,
            ),
        }

    def get_current_environment(self) -> Environment:
        """Get the current environment from environment variables."""
        env_name = os.environ.get("NADLANFIX_ENV", Environment.DEVELOPMENT.value).lower()
        try:
            return Environment(env_name)
        except ValueError:
            return Environment.DEVELOPMENT

    def get_config(self, environment: Environment | str | None = None) -> EnvironmentConfig:
        """Get configuration for a specific environment."""
        if environment is None:
            environment = self.get_current_environment()

        if isinstance(environment, Environment):
            env_name = environment.value
        else:
            env_name = environment

        return self._configs.get(env_name, self._configs[Environment.DEVELOPMENT.value])

    def get_database_url(self, environment: Environment | str | None = None) -> str:
        """Get database URL for a specific environment."""
        config = self.get_config(environment)
        return config.database_url

    def is_production(self) -> bool:
        """Check if current environment is production."""
        return self.get_current_environment() == Environment.PRODUCTION

    def is_development(self) -> bool:
        """Check if current environment is development."""
        return self.get_current_environment() == Environment.DEVELOPMENT

    def get_secret(self, key: str, default: str | None = None) -> str | None:
        """Get a secret from environment variables."""
        return os.environ.get(key, default)

    def validate_config(self, config: EnvironmentConfig) -> list[str]:
        """Validate configuration and return list of issues."""
        issues = []

        if not config.database_url:
            issues.append("database_url is required")

        if config.auth_required and not config.basic_auth:
            issues.append("basic_auth is required when auth_required is True")

        if config.scheduler_enabled and config.auto_cycle_hours < 1:
            issues.append("auto_cycle_hours must be at least 1")

        if config.max_workers < 1:
            issues.append("max_workers must be at least 1")

        if config.retention_days < 1:
            issues.append("retention_days must be at least 1")

        return issues
