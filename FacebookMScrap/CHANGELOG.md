# Changelog

All notable changes to this project are documented here.

## [0.5.0] - 2026-08-08

### Added

- Browser-side card snapshot extraction and JSON fixture capture/sanitization tooling.
- Heuristic category and condition classification.
- Optional low-concurrency local `llama.cpp` classification through `/v1/chat/completions`.
- Category-aware valuation profiles, comparable thresholds, sample targets, and condition adjustments.
- Listing-detail dashboard pages with dependency-free price-history SVG charts and score reasons.
- Deduplicated high-score/target-price notification events with log, JSONL, and webhook sinks.
- Queue-backed structured JSON/text logging.
- Configurable database/history retention and retention-run audit records.
- Search-run duration metrics and dashboard run/notification panels.
- Schema migration 3 for enrichment, score reasons, notifications, duration, and retention metadata.
- Regression tests for fixture records, metadata, category-aware pricing, notification dedupe/retention, and detail APIs.

### Changed

- Upgraded package version from 0.4.0 to 0.5.0.
- Runtime extraction uses one serialized browser snapshot rather than per-card Playwright attribute calls.
- Comparable-price statistics prefer same-category candidates and use category-specific thresholds.
- `httpx` is now a runtime dependency for optional local-LLM and webhook clients.

## [0.4.0] - 2026-08-08

### Added

- Deterministic fuzzy comparable-title matching for deal-score price evidence.
- Unit/spec normalization for common Marketplace title variants.
- Model-number guardrails so incompatible model tokens are not grouped as comparables.
- Regression coverage for comparable tokenization, model matching, unit aliases, and fuzzy historical price statistics.

### Changed

- Upgraded package version from 0.3.0 to 0.4.0.
- Comparable-price lookups use a broad indexed-title prefilter followed by weighted token similarity.
- Comparable candidates are capped to recent same-currency listings to keep scoring CPU and I/O bounded.

## [0.3.0] - 2026-08-08

### Added

- Versioned, idempotent SQLite migrations with an explicit schema version.
- Automatic in-place upgrade path for existing 0.2.x databases.
- Persistent daemon lifecycle, heartbeat, active-watchlist, success, and last-error metadata.
- Per-watchlist last-success and last-error metadata.
- Dashboard/API watchlist create, edit, enable/disable, and delete controls.
- Health API backed by real schema and daemon state.

## [0.2.0] - 2026-08-08

### Added

- End-to-end Marketplace collection pipeline.
- DOM extraction adapter using Marketplace item links.
- Listing normalization and canonical IDs/fingerprints.
- SQLite storage, deduplication, search-run audit data, and price history.
- Comparable-market deal scoring, persistent watchlists, daemon, browser session capture, dashboard, CI, and tests.

## [0.1.0] - 2026-08-08

### Added

- Initial Python 3.12 project scaffold, async Playwright wrapper, Typer CLI, Pydantic model, tests, and governance files.
