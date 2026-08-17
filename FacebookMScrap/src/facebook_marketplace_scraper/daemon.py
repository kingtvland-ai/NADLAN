# src/facebook_marketplace_scraper/daemon.py
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from time import monotonic

from .browser import MarketplaceBrowser
from .llm_classifier import LlamaClassifierSettings, LocalLlamaClassifier
from .models import SearchSpec, Watchlist
from .notifications import NotificationManager, NotificationSettings
from .retention import retention_policy_from_env
from .service import MarketplaceCollector
from .storage import MarketplaceStore

logger = logging.getLogger(__name__)


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:2000]


async def run_daemon(
    *,
    db_path: Path,
    storage_state_path: Path | None,
    poll_seconds: int = 30,
    headless: bool = True,
    once: bool = False,
) -> None:
    store = MarketplaceStore(db_path)
    await store.initialize()
    await store.daemon_started(os.getpid())
    llm_settings = LlamaClassifierSettings.from_env()
    classifier = LocalLlamaClassifier(llm_settings) if llm_settings.enabled else None
    notifier = NotificationManager(store, NotificationSettings.from_env())
    retention = retention_policy_from_env()
    next_retention = 0.0
    collector = MarketplaceCollector(
        store=store,
        storage_state_path=storage_state_path,
        headless=headless,
        classifier=classifier,
        notifier=notifier,
    )
    fatal_error: str | None = None

    try:
        if classifier is not None:
            healthy = await classifier.health()
            logger.info("local LLM classifier health=%s", healthy)
        async with MarketplaceBrowser(
            headless=headless,
            storage_state_path=storage_state_path,
        ) as browser:
            while True:
                await store.daemon_heartbeat()
                now = monotonic()
                if now >= next_retention:
                    try:
                        deleted = await store.prune(retention)
                        logger.info("retention completed: %s", deleted, extra={"event": "retention"})
                    except Exception:
                        logger.exception("retention maintenance failed")
                    next_retention = now + retention.interval_seconds

                watchlists = (
                    await store.list_watchlists(enabled_only=True)
                    if once
                    else await store.due_watchlists()
                )
                cycle_errors: list[str] = []
                for watchlist in watchlists:
                    await store.daemon_heartbeat(active_watchlist=watchlist.name)
                    try:
                        await _run_watchlist(collector, browser, watchlist)
                    except Exception as exc:
                        error = _error_text(exc)
                        cycle_errors.append(f"{watchlist.name}: {error}")
                        logger.exception(
                            "Watchlist %s failed",
                            watchlist.name,
                            extra={"watchlist": watchlist.name},
                        )
                        if watchlist.id is not None:
                            await store.mark_watchlist_run(
                                watchlist.id,
                                success=False,
                                error=error,
                            )
                    else:
                        if watchlist.id is not None:
                            await store.mark_watchlist_run(watchlist.id, success=True)
                    finally:
                        await store.daemon_heartbeat(active_watchlist=None)

                cycle_error = "; ".join(cycle_errors)[:2000] or None
                await store.daemon_cycle_completed(
                    success=cycle_error is None,
                    error=cycle_error,
                )
                if once:
                    return
                await asyncio.sleep(max(5, poll_seconds))
    except Exception as exc:
        fatal_error = _error_text(exc)
        logger.exception("Marketplace daemon stopped after a fatal error")
        raise
    finally:
        await notifier.close()
        if classifier is not None:
            await classifier.close()
        await store.daemon_stopped(error=fatal_error)


async def _run_watchlist(
    collector: MarketplaceCollector,
    browser: MarketplaceBrowser,
    watchlist: Watchlist,
) -> None:
    spec = SearchSpec(
        query=watchlist.query,
        city_id=watchlist.city_id,
        radius_km=watchlist.radius_km,
<<<<<<< HEAD
        category_id=watchlist.category_id,
=======
>>>>>>> 8845a77da1aebb7aa068a68ffdd3780ca1ea970a
        max_items=watchlist.max_items,
        min_price=watchlist.min_price,
        max_price=watchlist.max_price,
        default_currency=watchlist.default_currency,
    )
    await collector.collect(spec, watchlist=watchlist, browser=browser)
