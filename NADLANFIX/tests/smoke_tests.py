"""Smoke tests for NADLANFIX architecture.

Verifies that the core components are properly integrated:
- Source adapters can be instantiated
- Canonical model works
- Scheduler can be created
- Health monitor works
- Validation works
- Normalized store works
- Dedupe layer works
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import sys

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


def test_source_adapters():
    """Test that all source adapters can be instantiated."""
    from sources.base.adapter import BaseSourceAdapter
    from sources.yad2.adapter import Yad2SourceAdapter
    from sources.facebook.adapter import FacebookSourceAdapter
    from sources.onmap.adapter import OnmapSourceAdapter

    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
        db_path = f.name

    try:
        for adapter_class in [Yad2SourceAdapter, FacebookSourceAdapter, OnmapSourceAdapter]:
            adapter = adapter_class(db_path)
            assert adapter.source_name, f"{adapter_class.__name__} missing source_name"
            assert adapter.display_name, f"{adapter_class.__name__} missing display_name"
            print(f"  [OK] {adapter_class.__name__}: {adapter.display_name}")
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_canonical_model():
    """Test that the canonical model can be created and serialized."""
    from sources.base.models import CanonicalListing

    listing = CanonicalListing(
        listing_id="test:123",
        source="test",
        external_id="123",
        title="Test Property",
        city="Tel Aviv",
        price=1000000,
        rooms=3,
        area_sqm=80,
    )

    assert listing.listing_id == "test:123"
    assert listing.source == "test"
    assert listing.price == 1000000

    # Test serialization
    data = listing.to_dict()
    assert data["listing_id"] == "test:123"
    assert data["price"] == 1000000

    # Test deserialization
    restored = CanonicalListing.from_dict(data)
    assert restored.listing_id == listing.listing_id
    assert restored.price == listing.price

    print("  [OK] CanonicalListing: create, serialize, deserialize")


def test_validation():
    """Test that validation catches bad data."""
    from jobs.validation import ListingValidator

    validator = ListingValidator()

    # Valid listing
    result = validator.validate({
        "listing_id": "test:1",
        "source": "test",
        "external_id": "1",
        "title": "Good Property",
        "city": "Tel Aviv",
        "price": 1000000,
    })
    assert result.is_valid, f"Valid listing failed: {result.errors}"

    # Invalid listing - missing required field
    result = validator.validate({
        "listing_id": "test:2",
        "source": "test",
        "external_id": "2",
        "title": "",
        "city": "",
    })
    assert not result.is_valid, "Invalid listing passed"
    assert "Missing required field" in str(result.errors)

    print("  [OK] ListingValidator: valid and invalid cases")


def test_dedupe_layer():
    """Test that deduplication works."""
    from pipeline.dedupe import DedupeLayer

    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
        db_path = f.name

    try:
        dedupe = DedupeLayer(db_path)

        # First listing - should be new
        result1 = dedupe.resolve("yad2", "token123", "telaviv:main:1000000:3:80")
        assert result1.is_new
        assert result1.canonical_id == "yad2:token123"

        # Same listing again - should be existing
        result2 = dedupe.resolve("yad2", "token123")
        assert not result2.is_new
        assert result2.canonical_id == result1.canonical_id

        # Different source, same fingerprint - should merge into existing
        result3 = dedupe.resolve("facebook", "fb456", "telaviv:main:1000000:3:80")
        assert not result3.is_new  # merged into existing yad2 canonical
        assert result3.canonical_id == "yad2:token123"  # merged into yad2
        assert "facebook" in result3.merged_sources

        # Check stats
        stats = dedupe.count()
        assert stats["canonical_listings"] == 1  # facebook merged into yad2
        assert stats["cross_source_links"] == 1

        print("  [OK] DedupeLayer: new, existing, cross-source merge")
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_normalized_store():
    """Test that the normalized store works."""
    from storage.sqlite_local import NormalizedStore

    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
        db_path = f.name

    try:
        store = NormalizedStore(db_path)

        # Insert a listing
        listing = {
            "canonical_id": "yad2:test123",
            "source": "yad2",
            "external_id": "test123",
            "title": "Test Property",
            "city": "Tel Aviv",
            "price": 1000000,
            "rooms": 3,
            "area_sqm": 80,
        }
        inserted = store.upsert(listing)
        assert inserted, "First insert should return True"

        # Update the same listing
        listing["price"] = 950000
        updated = store.upsert(listing)
        assert not updated, "Update should return False"

        # Get the listing
        retrieved = store.get("yad2:test123")
        assert retrieved is not None
        assert retrieved["price"] == 950000

        # Count
        count = store.count()
        assert count["total"] == 1

        print("  [OK] NormalizedStore: insert, update, get, count")
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_health_monitor():
    """Test that health monitoring works."""
    from jobs.health import HealthMonitor

    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
        db_path = f.name

    try:
        monitor = HealthMonitor(db_path)

        # Record success
        monitor.record_success("yad2", duration_seconds=10.0)
        health = monitor.get_health("yad2")
        assert health.status.value == "healthy"
        assert health.last_success_at is not None

        # Record failure
        monitor.record_failure("facebook", "Connection timeout")
        health = monitor.get_health("facebook")
        assert health.status.value == "failed"
        assert health.last_error == "Connection timeout"

        # Get all health
        all_health = monitor.get_all_health()
        assert len(all_health) == 2

        print("  [OK] HealthMonitor: record success/failure, get health")
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_scheduler_migrates_legacy_schema():
    """Legacy scheduler schema should auto-migrate to the modern layout."""
    from jobs.scheduler import UnifiedScheduler

    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
        db_path = f.name

    try:
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE scheduler_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                ok INTEGER NOT NULL DEFAULT 0,
                skipped INTEGER NOT NULL DEFAULT 0,
                detail TEXT,
                error TEXT
            );
            INSERT INTO scheduler_runs (job, started_at, finished_at, ok, skipped, detail, error)
            VALUES ('yad2', '2024-01-01T00:00:00+00:00', '2024-01-01T00:05:00+00:00', 1, 0, '{"rows": 3}', NULL);
        """)
        conn.commit()
        conn.close()

        scheduler = UnifiedScheduler(db_path)
        conn2 = sqlite3.connect(db_path)
        try:
            row = conn2.execute(
                "SELECT job_name, status, source_name, error FROM scheduler_runs WHERE job_name = 'yad2'"
            ).fetchone()
            assert row is not None, "Legacy row was not migrated"
            assert row[0] == "yad2"
            assert row[1] == "success"
            assert row[2] is None
        finally:
            conn2.close()
        print("  [OK] UnifiedScheduler: legacy schema migration")
    finally:
        try:
            Path(db_path).unlink(missing_ok=True)
        except PermissionError:
            pass


def test_scheduler():
    """Test that the scheduler can be created and used."""
    from jobs.scheduler import UnifiedScheduler, SchedulerConfig

    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
        db_path = f.name

    try:
        config = SchedulerConfig(max_retries=1, retry_delay_seconds=0.1)
        scheduler = UnifiedScheduler(db_path, config)

        # Get health (should be empty)
        health = scheduler.get_all_health()
        assert isinstance(health, list)

        # Get recent runs (should be empty)
        runs = scheduler.get_recent_runs()
        assert isinstance(runs, list)

        print("  [OK] UnifiedScheduler: create, get health, get runs")
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_schema_versioning():
    """Test that schema versioning works."""
    from storage.schema_versioning import SchemaVersioning

    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
        db_path = f.name

    try:
        versioning = SchemaVersioning(db_path)

        # Initially no version
        version = versioning.get_current_version()
        assert version is None

        # Set version
        versioning.set_version("1.0.0", "Initial schema")
        version = versioning.get_current_version()
        assert version == "1.0.0"

        # Ensure version
        ensured = versioning.ensure_version()
        assert ensured == "1.0.0"

        # Metadata
        versioning.set_metadata("test_key", "test_value")
        value = versioning.get_metadata("test_key")
        assert value == "test_value"

        print("  [OK] SchemaVersioning: set/get version, metadata")
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_rbac():
    """Test that RBAC works."""
    from app.auth.rbac import RBAC, Role, Permission, User

    rbac = RBAC()

    # Create users
    user = User(user_id=1, username="user", role=Role.USER)
    agent = User(user_id=2, username="agent", role=Role.AGENT)
    admin = User(user_id=3, username="admin", role=Role.ADMIN)

    rbac.register_user(user)
    rbac.register_user(agent)
    rbac.register_user(admin)

    # Test permissions
    assert rbac.check_permission(1, Permission.VIEW_LISTINGS) is True
    assert rbac.check_permission(1, Permission.MANAGE_USERS) is False

    assert rbac.check_permission(2, Permission.VIEW_LEADS) is True
    assert rbac.check_permission(2, Permission.MANAGE_USERS) is False

    assert rbac.check_permission(3, Permission.MANAGE_USERS) is True
    assert rbac.check_permission(3, Permission.RUN_INGESTION) is True

    # Test layer access
    assert rbac.can_access(1, "user") is True
    assert rbac.can_access(1, "admin") is False
    assert rbac.can_access(1, "crm") is False

    assert rbac.can_access(2, "user") is True
    assert rbac.can_access(2, "crm") is True
    assert rbac.can_access(2, "admin") is False

    assert rbac.can_access(3, "admin") is True

    print("  [OK] RBAC: roles, permissions, layer access")


def test_crm_backend():
    """Test that CRM backend works."""
    from app.crm.crm_backend import CRMBackend, Lead, Task, Note, LeadStatus, TaskStatus, TaskPriority

    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
        db_path = f.name

    try:
        crm = CRMBackend(db_path)

        # Create lead
        lead = Lead(
            lead_id=0,
            title="Test Lead",
            description="Test description",
            status=LeadStatus.NEW,
            source="yad2",
            city="Tel Aviv",
            price=1000000,
            contact_name="John Doe",
            phone="050-1234567",
            owner_id=1,
        )
        lead_id = crm.create_lead(lead)
        assert lead_id > 0

        # Get lead
        retrieved = crm.get_lead(lead_id)
        assert retrieved is not None
        assert retrieved["title"] == "Test Lead"

        # Update lead
        updated = crm.update_lead(lead_id, {"status": "contacted"}, user_id=1)
        assert updated is True

        # Create task
        task = Task(
            task_id=0,
            title="Call lead",
            description="Follow up",
            status=TaskStatus.PENDING,
            priority=TaskPriority.HIGH,
            lead_id=lead_id,
            created_by=1,
        )
        task_id = crm.create_task(task)
        assert task_id > 0

        # Create note
        note = Note(
            note_id=0,
            lead_id=lead_id,
            content="Test note",
            created_by=1,
        )
        note_id = crm.create_note(note)
        assert note_id > 0

        # Get stats
        stats = crm.get_stats()
        assert stats["leads_total"] == 1
        assert stats["tasks_total"] == 1
        assert stats["notes_total"] == 1

        print("  [OK] CRMBackend: leads, tasks, notes, stats")
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_observability():
    """Test that observability works."""
    from observability.metrics import Observability, Metric, Alert

    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
        db_path = f.name

    try:
        obs = Observability(db_path)

        # Record metric
        obs.record_metric(Metric(name="test_metric", value=42.0, tags={"source": "yad2"}))
        metrics = obs.get_metrics("test_metric")
        assert len(metrics) == 1
        assert metrics[0]["value"] == 42.0

        # Record request
        obs.record_request("GET", "/api/listings", 200, 150.5, user_id=1)
        stats = obs.get_request_stats()
        assert stats["total"] == 1

        # Record job
        obs.record_job("yad2", "success", datetime.now(timezone.utc).isoformat(), rows_processed=100)
        jobs = obs.get_job_stats("yad2")
        assert len(jobs) == 1
        assert jobs[0]["rows_processed"] == 100

        # Create alert
        alert = Alert(
            alert_id="alert_1",
            severity="warning",
            message="Test alert",
            source="yad2",
        )
        obs.create_alert(alert)
        alerts = obs.get_active_alerts()
        assert len(alerts) == 1

        print("  [OK] Observability: metrics, requests, jobs, alerts")
    finally:
        Path(db_path).unlink(missing_ok=True)


def test_api_versioning():
    """Test that API versioning works."""
    from app.api.versioning import APIVersioning, APIVersion

    versioning = APIVersioning()

    # Register route
    def handler():
        return {"ok": True}

    versioning.register_route("v1", "/listings", handler)
    versioning.register_route("v2", "/listings", handler)

    # Get route
    assert versioning.get_route("v1", "/listings") is not None
    assert versioning.get_route("v2", "/listings") is not None
    assert versioning.get_route("v1", "/unknown") is None

    # Version extraction
    version, path = versioning.get_version_from_path("/api/v1/listings")
    assert version == "v1"
    assert path == "/listings"

    version, path = versioning.get_version_from_path("/api/listings")
    assert version == "v1"
    assert path == "/api/listings"  # No version prefix, returns full path

    # Get all versions
    versions = versioning.get_all_versions()
    assert len(versions) == 2

    print("  [OK] APIVersioning: register, get, version extraction")


def test_caching():
    """Test that caching works."""
    from app.api.caching import Cache, ReadOptimizer

    cache = Cache(default_ttl=60)

    # Set and get
    cache.set("key1", "value1")
    assert cache.get("key1") == "value1"

    # Miss
    assert cache.get("nonexistent") is None

    # Delete
    cache.set("key2", "value2")
    cache.delete("key2")
    assert cache.get("key2") is None

    # Stats
    cache.get("key1")
    cache.get("nonexistent")
    stats = cache.get_stats()
    assert stats["hits"] >= 1
    assert stats["misses"] >= 1

    # Pattern invalidation
    cache.set("listings:1", {})
    cache.set("listings:2", {})
    cache.set("other:1", {})
    invalidated = cache.invalidate_pattern("listings:")
    assert invalidated == 2
    assert cache.get("listings:1") is None
    assert cache.get("other:1") is not None

    print("  [OK] Cache: set/get/delete, stats, pattern invalidation")


def test_backup_manager():
    """Test that backup manager works."""
    from storage.backup import BackupManager

    with tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False) as f:
        db_path = f.name

    try:
        backup_dir = Path(db_path).parent / "backups"
        backup_dir.mkdir(exist_ok=True)
        manager = BackupManager(db_path, backup_dir)

        # Create backup
        backup = manager.create_backup("full")
        assert backup.backup_id.startswith("backup_")
        assert Path(backup.file_path).exists()

        # List backups
        backups = manager.list_backups()
        assert len(backups) == 1
        assert backups[0].backup_id == backup.backup_id

        # Cleanup old backups
        removed = manager.cleanup_old_backups(keep_days=0)
        assert removed == 1

        print("  [OK] BackupManager: create, list, cleanup")
    finally:
        Path(db_path).unlink(missing_ok=True)
        import shutil
        if Path(db_path).parent.joinpath("backups").exists():
            shutil.rmtree(Path(db_path).parent.joinpath("backups"))


def test_environment_config():
    """Test that environment config works."""
    from config.environment import EnvironmentManager, Environment, EnvironmentConfig

    manager = EnvironmentManager()

    # Get current environment
    env = manager.get_current_environment()
    assert isinstance(env, Environment)

    # Get config
    config = manager.get_config(Environment.DEVELOPMENT)
    assert config.name == "development"
    assert config.debug is True
    assert config.auth_required is False

    # Test with a config that has auth
    auth_config = EnvironmentConfig(
        name="test",
        database_url=":memory:",
        auth_required=True,
        basic_auth="test_auth",
    )
    issues = manager.validate_config(auth_config)
    assert len(issues) == 0

    # Test invalid config
    invalid_config = EnvironmentConfig(
        name="test",
        database_url="",
        auth_required=True,
        basic_auth=None,
    )
    issues = manager.validate_config(invalid_config)
    assert len(issues) > 0

    print("  [OK] EnvironmentManager: get config, validate")


def run_all_tests():
    """Run all smoke tests."""
    print("=" * 60)
    print("NADLANFIX Smoke Tests")
    print("=" * 60)

    tests = [
        ("Source Adapters", test_source_adapters),
        ("Canonical Model", test_canonical_model),
        ("Validation", test_validation),
        ("Dedupe Layer", test_dedupe_layer),
        ("Normalized Store", test_normalized_store),
        ("Health Monitor", test_health_monitor),
        ("Scheduler", test_scheduler),
        ("Schema Versioning", test_schema_versioning),
        ("RBAC", test_rbac),
        ("CRM Backend", test_crm_backend),
        ("Observability", test_observability),
        ("API Versioning", test_api_versioning),
        ("Caching", test_caching),
        ("Backup Manager", test_backup_manager),
        ("Environment Config", test_environment_config),
    ]

    passed = 0
    failed = 0

    for name, test_fn in tests:
        print(f"\n{name}:")
        try:
            test_fn()
            passed += 1
        except Exception as exc:
            print(f"  [FAIL] {exc}")
            failed += 1

    print("\n" + "=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)

    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
