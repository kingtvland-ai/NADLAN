# FacebookMarketplaceScraper

Async-first Python 3.12+ Marketplace research toolkit:

**search → snapshot/extract → normalize → metadata classification → SQLite → deduplicate → historical pricing → category-aware valuation → watchlists → notifications → daemon → dashboard**

The collector reads Marketplace content available to the browser session you provide. It does not include CAPTCHA bypass, credential theft, access-control bypass, or anti-abuse evasion.

## Features

- One browser-side result-card snapshot per search instead of many Playwright round-trips.
- Sanitized JSON fixture capture using the same record contract as production extraction.
- Deterministic category and condition classification with optional local `llama.cpp` refinement.
- Category-aware fuzzy comparables, thresholds, sample targets, and scoring weights.
- Listing-ID deduplication and compact historical price tracking.
- Versioned SQLite migrations; existing databases upgrade in place.
- Queue-backed structured text/JSON logging so log writes are moved off the asyncio event loop.
- Persistent watchlists, high-score/target-price notifications, and notification deduplication.
- Optional JSONL and generic webhook notification sinks.
- Configurable price/search/notification/listing retention with retention-run audit data.
- Durable daemon heartbeat/error state and search-run duration metrics.
- Dashboard listing-detail pages with price-history SVG charts, score reasons, metadata, notifications, and run timing.
- Resource-conscious defaults: one browser, sequential watchlists, one local-LLM classification request at a time, bounded comparable candidates.

## Install

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e '.[dev]'
playwright install chromium
fb-market init-db
```

## Browser session

```bash
fb-market session login
```

The resulting `data/facebook_storage_state.json` is ignored by Git and reused for later searches. Do not commit or share it.

## Capture a real extraction fixture

With an authenticated browser session available:

```bash
fb-market fixture capture "thinkpad" \
  --output tests/fixtures/marketplace_search_cards.json \
  --max-items 30
```

The command snapshots the real result-card anchors using the production extractor, removes query strings, substitutes unstable item IDs, replaces remote image URLs, and writes JSON that can be committed safely for extractor regression testing. CI consumes the same record shape without needing Chromium.

## Search

```bash
fb-market search "thinkpad" --max-items 50
fb-market search "solar panel" --min-price 25 --max-price 500 --max-items 100
```

Results are enriched with category/condition metadata, persisted, deduplicated, compared against same-category fuzzy peers, scored, and checked for notification rules.

## Optional local llama.cpp classification

`llama-server` currently exposes an OpenAI-compatible chat-completions endpoint. Start your existing local server, then configure the collector:

```bash
export FBMS_LLM_ENABLED=1
export FBMS_LLM_URL=http://127.0.0.1:8080/v1
export FBMS_LLM_MODEL=local
fb-market llm status
```

The classifier is optional. Heuristic metadata is always available, inference concurrency is capped at one request, and failures fall back to heuristic metadata with a cooldown rather than blocking collection.

Useful variables:

```text
FBMS_LLM_ENABLED=0|1
FBMS_LLM_URL=http://127.0.0.1:8080/v1
FBMS_LLM_MODEL=local
FBMS_LLM_TIMEOUT=20
FBMS_LLM_UNCERTAIN_ONLY=1
```

## Watchlists and daemon

```bash
fb-market watch add --name laptops --query "thinkpad" --target-price 250 --interval-minutes 30
fb-market watch list
fb-market daemon --once
fb-market daemon
```

The daemon reuses one browser and one optional LLM client, runs watchlists sequentially, records durable health state, and periodically applies retention.

## Notifications

High-score and target-price events are logged by default and deduplicated by listing/watchlist/event/price. Optional sinks:

```bash
export FBMS_NOTIFY_MIN_SCORE=75
export FBMS_NOTIFY_JSONL=data/notifications.jsonl
export FBMS_NOTIFY_WEBHOOK_URL=https://your-internal-endpoint.example/marketplace
```

Listings excluded by safety classification are not scored, stored, surfaced, or notified.

## Retention

Defaults are conservative and configurable:

```text
FBMS_RETENTION_PRICE_DAYS=365
FBMS_RETENTION_RUN_DAYS=90
FBMS_RETENTION_NOTIFICATION_DAYS=90
FBMS_RETENTION_LISTING_DAYS=365
FBMS_RETENTION_INTERVAL_SECONDS=21600
```

Run maintenance immediately:

```bash
fb-market maintenance prune
```

The latest price point for each listing is retained even if it is older than the history cutoff.

## Structured logging

```bash
fb-market --log-format json --log-level INFO daemon
```

Application log calls enqueue records and a listener thread performs the actual stream write.

## Dashboard

```bash
fb-market dashboard --host 127.0.0.1 --port 8787
```

Open `http://127.0.0.1:8787/`. The main dashboard shows stats, health, listings, category/condition metadata, notifications, watchlists, and recent run timing. Clicking a listing opens its detail page and price-history chart.

API endpoints include:

- `GET /api/health`
- `GET /api/stats`
- `GET /api/listings?limit=100`
- `GET /api/listings/{listing_id}`
- `GET /api/listings/{listing_id}/history`
- `GET /api/notifications`
- `GET /api/runs`
- `GET /api/watchlists`
- `POST /api/watchlists`
- `PATCH /api/watchlists/{watchlist_id}`
- `DELETE /api/watchlists/{watchlist_id}`

## SQLite data

- `schema_migrations`: applied schema versions.
- `listings`: latest listing state, classification metadata, score, and score reasons.
- `listing_prices`: initial price plus later price changes.
- `search_runs`: audit counters plus duration.
- `search_run_listings`: run-to-listing membership.
- `watchlists`: recurring searches and per-watchlist health state.
- `watchlist_matches`: listings observed by each watchlist.
- `notification_events`: deduplicated delivered notification events.
- `retention_runs`: maintenance audit history.
- `daemon_status`: durable daemon heartbeat/lifecycle/error record.

## Development

```bash
pytest
ruff check .
python -m compileall src tests
```

CI runs Ruff, bytecode compilation, and the full test suite on Python 3.12 and Python 3.13.
