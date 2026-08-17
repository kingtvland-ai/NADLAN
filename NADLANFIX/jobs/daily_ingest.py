"""Daily ingestion orchestration for NADLANFIX.

This module provides the main entry point for daily data ingestion.
It coordinates all sources, handles errors, and ensures the system
runs reliably on a schedule.

Usage:
    python jobs/daily_ingest.py run --sources yad2,facebook,onmap
    python jobs/daily_ingest.py run --all
    python jobs/daily_ingest.py status
    python jobs/daily_ingest.py health
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from jobs.scheduler import UnifiedScheduler, SchedulerConfig
from jobs.health import HealthMonitor

# Source adapters
from sources.yad2.adapter import Yad2SourceAdapter
from sources.facebook.adapter import FacebookSourceAdapter
from sources.onmap.adapter import OnmapSourceAdapter


# Default configuration
DEFAULT_DB_PATH = Path(__file__).parent.parent / "data" / "planwatch.sqlite3"
DEFAULT_SOURCES = {
    "yad2": {"target": 18000, "enabled": True},
    "facebook": {"target": 500, "enabled": True},
    "onmap": {"target": 5000, "enabled": True},
}


def run_daily_ingest(
    db_path: Path | str = DEFAULT_DB_PATH,
    sources: dict[str, dict] | None = None,
    config: SchedulerConfig | None = None,
) -> dict:
    """Run daily ingestion for all enabled sources.

    Args:
        db_path: Path to SQLite database
        sources: Dict of source_name -> {target, enabled}
        config: Scheduler configuration

    Returns:
        Dict with ingestion results
    """
    db_path = Path(db_path)
    sources = sources or DEFAULT_SOURCES
    config = config or SchedulerConfig()

    scheduler = UnifiedScheduler(db_path, config)
    health_monitor = HealthMonitor(db_path)

    # Build source adapters
    adapters = {}
    for source_name, source_config in sources.items():
        if not source_config.get("enabled", True):
            continue

        if source_name == "yad2":
            adapters[source_name] = Yad2SourceAdapter(db_path)
        elif source_name == "facebook":
            adapters[source_name] = FacebookSourceAdapter(db_path)
        elif source_name == "onmap":
            adapters[source_name] = OnmapSourceAdapter(db_path)
        else:
            print(f"Unknown source: {source_name}")
            continue

    # Run daily cycle
    print(f"Starting daily ingestion at {datetime.now(timezone.utc).isoformat()}")
    print(f"Sources: {', '.join(adapters.keys())}")

    results = scheduler.run_daily_cycle(adapters, {
        name: cfg["target"] for name, cfg in sources.items() if cfg.get("enabled", True)
    })

    # Update health monitor
    for result in results:
        source_name = result.metadata.get("source_name", result.job_name)
        if result.status.value == "success":
            health_monitor.record_success(source_name, result.duration_seconds, result.metadata)
        else:
            health_monitor.record_failure(source_name, result.error or "Unknown error", result.metadata)

    # Print summary
    print("\n" + "=" * 60)
    print("DAILY INGESTION SUMMARY")
    print("=" * 60)

    for result in results:
        status_icon = "✓" if result.status.value == "success" else "✗"
        print(f"{status_icon} {result.job_name}: {result.status.value}")
        if result.error:
            print(f"  Error: {result.error}")
        print(f"  Rows: {result.rows_processed} | Duration: {result.duration_seconds:.1f}s")

    print("=" * 60)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "results": [r.to_dict() for r in results],
        "summary": {
            "total": len(results),
            "success": sum(1 for r in results if r.status.value == "success"),
            "failed": sum(1 for r in results if r.status.value == "failed"),
            "total_rows": sum(r.rows_processed for r in results),
        },
    }


def main():
    parser = argparse.ArgumentParser(description="NADLANFIX Daily Ingestion")
    parser.add_argument("command", choices=["run", "status", "health"], help="Command to run")
    parser.add_argument("--sources", help="Comma-separated list of sources")
    parser.add_argument("--all", action="store_true", help="Run all sources")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Database path")
    parser.add_argument("--target", type=int, default=500, help="Target listings per source")

    args = parser.parse_args()

    if args.command == "run":
        sources = DEFAULT_SOURCES.copy()
        if args.sources:
            enabled = {s.strip() for s in args.sources.split(",")}
            for name in sources:
                sources[name]["enabled"] = name in enabled
        elif not args.all:
            parser.error("Either --sources or --all is required")

        result = run_daily_ingest(
            db_path=args.db,
            sources=sources,
        )
        print(result["summary"])
        sys.exit(0 if result["summary"]["failed"] == 0 else 1)

    elif args.command == "status":
        scheduler = UnifiedScheduler(args.db)
        runs = scheduler.get_recent_runs(limit=20)
        for run in runs:
            print(f"{run['job_name']}: {run['status']} at {run['started_at']}")
        sys.exit(0)

    elif args.command == "health":
        monitor = HealthMonitor(args.db)
        health = monitor.get_all_health()
        for h in health:
            print(f"{h.source_name}: {h.status.value} (success rate: {h.success_rate:.0%})")
        sys.exit(0)


if __name__ == "__main__":
    main()
