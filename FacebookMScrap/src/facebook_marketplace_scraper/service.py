# src/facebook_marketplace_scraper/service.py
from __future__ import annotations

import logging
from pathlib import Path
from time import perf_counter

from .browser import MarketplaceBrowser
from .extractor import MarketplaceDomExtractor
from .llm_classifier import LocalLlamaClassifier
from .models import CollectionResult, MarketplaceListing, SearchSpec, Watchlist
from .normalizer import normalize_raw_listing
from .notifications import NotificationManager
from .scoring import score_listing
from .storage import MarketplaceStore

logger = logging.getLogger(__name__)


class MarketplaceCollector:
    def __init__(
        self,
        *,
        store: MarketplaceStore,
        extractor: MarketplaceDomExtractor | None = None,
        storage_state_path: Path | None = None,
        headless: bool = True,
        classifier: LocalLlamaClassifier | None = None,
        notifier: NotificationManager | None = None,
    ) -> None:
        self.store = store
        self.extractor = extractor or MarketplaceDomExtractor()
        self.storage_state_path = storage_state_path
        self.headless = headless
        self.classifier = classifier
        self.notifier = notifier

    async def collect(
        self,
        spec: SearchSpec,
        *,
        watchlist: Watchlist | None = None,
        browser: MarketplaceBrowser | None = None,
    ) -> CollectionResult:
        if not spec.query.strip():
            raise ValueError("query must not be empty")
        if not spec.city_id.strip():
            raise ValueError("city_id must not be empty")
        started = perf_counter()
        await self.store.initialize()
        run_id = await self.store.start_search_run(spec.query)

        if browser is None:
            async with MarketplaceBrowser(
                headless=self.headless,
                storage_state_path=self.storage_state_path,
            ) as owned_browser:
                raw = await self._extract(owned_browser, spec)
        else:
            raw = await self._extract(browser, spec)

        normalized: list[MarketplaceListing] = []
        for item in raw:
            listing = normalize_raw_listing(
                item,
                query=spec.query,
                default_currency=spec.default_currency,
            )
            if self.classifier is not None:
                classification = await self.classifier.classify(listing)
                if classification is not None:
                    listing = listing.model_copy(
                        update={
                            "category": classification.category,
                            "condition": classification.condition,
                            "classification_source": classification.source,
                            "classification_confidence": classification.confidence,
                            "restricted": classification.restricted,
                        }
                    )
            if listing.restricted:
                logger.info(
                    "listing excluded by safety classification",
                    extra={"listing_id": listing.listing_id, "run_id": run_id},
                )
                continue
            if spec.min_price is not None and (
                listing.price_value is None or listing.price_value < spec.min_price
            ):
                continue
            if spec.max_price is not None and (
                listing.price_value is None or listing.price_value > spec.max_price
            ):
                continue
            normalized.append(listing)

        inserted = 0
        updated = 0
        price_changes = 0
        notifications = 0
        scored = []
        for listing in normalized:
            was_inserted, price_changed = await self.store.upsert_listing(
                listing, run_id=run_id, city_id=spec.city_id
            )
            inserted += int(was_inserted)
            updated += int(not was_inserted)
            price_changes += int(price_changed)
            stats = await self.store.price_stats(listing)
            item_score = score_listing(listing, stats, watchlist=watchlist)
            await self.store.update_score(
                listing.listing_id,
                item_score.deal_score,
                item_score.confidence,
                item_score.reasons,
            )
            if watchlist and watchlist.id is not None:
                await self.store.record_watchlist_match(
                    watchlist.id,
                    listing.listing_id,
                    item_score.deal_score,
                )
            if self.notifier is not None:
                try:
                    notifications += int(await self.notifier.maybe_notify(item_score, watchlist))
                except Exception:
                    logger.exception(
                        "notification delivery failed",
                        extra={"listing_id": listing.listing_id, "run_id": run_id},
                    )
            scored.append(item_score)

        scored.sort(key=lambda item: item.deal_score, reverse=True)
        duration_ms = round((perf_counter() - started) * 1000.0, 2)
        await self.store.finish_search_run(
            run_id,
            extracted=len(raw),
            normalized=len(normalized),
            inserted=inserted,
            updated=updated,
            price_changes=price_changes,
            duration_ms=duration_ms,
        )
        logger.info(
            "collection completed query=%r extracted=%d normalized=%d duration_ms=%.2f",
            spec.query,
            len(raw),
            len(normalized),
            duration_ms,
            extra={"run_id": run_id, "duration_ms": duration_ms},
        )
        return CollectionResult(
            query=spec.query,
            run_id=run_id,
            extracted=len(raw),
            normalized=len(normalized),
            inserted=inserted,
            updated=updated,
            price_changes=price_changes,
            notifications=notifications,
            listings=scored,
        )

    async def _extract(self, browser: MarketplaceBrowser, spec: SearchSpec):
<<<<<<< HEAD
        page = await browser.open_search_page(
            spec.query,
            spec.city_id,
            spec.min_price,
            spec.max_price,
            spec.category_id,
        )
=======
        page = await browser.open_search_page(spec.query, spec.city_id, spec.radius_km)
>>>>>>> 8845a77da1aebb7aa068a68ffdd3780ca1ea970a
        try:
            return await self.extractor.extract(page, max_items=spec.max_items)
        finally:
            await page.close()
