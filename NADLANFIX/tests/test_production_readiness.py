from __future__ import annotations

import sqlite3
from pathlib import Path

from config.environment import EnvironmentManager
from config.production import RetentionPolicy


def test_environment_manager_uses_repo_root():
    manager = EnvironmentManager(Path(__file__).resolve().parents[1])
    config = manager.get_config("production")
    assert "planwatch.sqlite3" in config.database_url
    assert Path(config.database_url).parent.name == "data"


def test_retention_policy_uses_valid_cutoff(tmp_path):
    db_path = tmp_path / "retention.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, created_at TEXT)")
    conn.execute("INSERT INTO events (created_at) VALUES ('2024-01-01T00:00:00+00:00')")
    conn.commit()
    conn.close()

    policy = RetentionPolicy(db_path)
    deleted = policy.enforce_retention("events", "created_at", 30)
    assert deleted == 1
