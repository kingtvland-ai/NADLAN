# src/facebook_marketplace_scraper/retention.py
from __future__ import annotations

import os

from .models import RetentionPolicy


def retention_policy_from_env() -> RetentionPolicy:
    return RetentionPolicy(
        price_history_days=int(os.getenv("FBMS_RETENTION_PRICE_DAYS", "365")),
        search_run_days=int(os.getenv("FBMS_RETENTION_RUN_DAYS", "90")),
        notification_days=int(os.getenv("FBMS_RETENTION_NOTIFICATION_DAYS", "90")),
        listing_days=int(os.getenv("FBMS_RETENTION_LISTING_DAYS", "365")),
        interval_seconds=int(os.getenv("FBMS_RETENTION_INTERVAL_SECONDS", "21600")),
    )
