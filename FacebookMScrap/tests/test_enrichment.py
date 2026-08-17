# tests/test_enrichment.py
from pathlib import Path

from facebook_marketplace_scraper.extractor import records_to_raw_listings
from facebook_marketplace_scraper.fixtures import load_fixture_records, sanitize_fixture_records
from facebook_marketplace_scraper.llm_classifier import parse_classifier_json
from facebook_marketplace_scraper.metadata import infer_metadata
from facebook_marketplace_scraper.models import MarketplaceListing, PriceStats
from facebook_marketplace_scraper.normalizer import normalize_raw_listing
from facebook_marketplace_scraper.scoring import score_listing
from facebook_marketplace_scraper.valuation import valuation_profile

FIXTURE = Path(__file__).parent / "fixtures" / "marketplace_search_cards.json"


def test_fixture_records_use_production_parser_and_dedupe() -> None:
    records = load_fixture_records(FIXTURE)
    raw = records_to_raw_listings(records, max_items=20)
    assert len(raw) == 2
    first = normalize_raw_listing(raw[0], query="thinkpad")
    assert first.listing_id == "900000000001"
    assert first.category == "computers"
    assert first.price_value == 225.0


def test_fixture_sanitizer_replaces_unstable_identifiers() -> None:
    records = [{"href": "/marketplace/item/12345/?tracking=x", "image_url": "https://cdn.example/a.jpg"}]
    sanitized = sanitize_fixture_records(records)
    assert sanitized[0]["href"] == "/marketplace/item/900000000001/"
    assert sanitized[0]["image_url"].startswith("https://example.invalid/")


def test_heuristic_category_and_condition() -> None:
    result = infer_metadata(title="ThinkPad T480", body="Excellent condition, works great")
    assert result.category == "computers"
    assert result.condition == "like_new"
    assert result.confidence >= 0.75


def test_llm_json_parser_is_bounded_to_known_labels() -> None:
    parsed = parse_classifier_json('```json\n{"category":"computers","condition":"good","confidence":0.91,"restricted":false}\n```')
    assert parsed.category == "computers"
    assert parsed.condition == "good"
    assert parsed.confidence == 0.91
    fallback = parse_classifier_json('{"category":"made-up","condition":"???","confidence":5}')
    assert fallback.category == "other"
    assert fallback.condition == "unknown"
    assert fallback.confidence == 1.0


def _listing(**updates) -> MarketplaceListing:
    data = dict(
        listing_id="100",
        title="ThinkPad T480",
        normalized_title="thinkpad t480",
        fingerprint="f",
        url="https://www.facebook.com/marketplace/item/100/",
        price_text="$200",
        price_value=200.0,
        currency="CAD",
        category="computers",
        condition="good",
        source_query="thinkpad",
    )
    data.update(updates)
    return MarketplaceListing(**data)


def test_category_profile_changes_confidence_target() -> None:
    profile = valuation_profile("computers")
    scored = score_listing(
        _listing(),
        PriceStats(
            sample_size=profile.sample_target,
            median_price=300,
            category="computers",
            similarity_threshold=profile.comparable_threshold,
            sample_target=profile.sample_target,
        ),
    )
    assert scored.confidence == 1.0
    assert scored.deal_score > 70
    assert any("computers comparable median" in reason for reason in scored.reasons)


def test_restricted_listing_is_not_scored() -> None:
    scored = score_listing(_listing(restricted=True), PriceStats())
    assert scored.deal_score == 0
    assert scored.confidence == 0
