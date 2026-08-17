# tests/test_scoring.py
from facebook_marketplace_scraper.models import MarketplaceListing, PriceStats, Watchlist
from facebook_marketplace_scraper.scoring import score_listing


def _listing(price: float) -> MarketplaceListing:
    return MarketplaceListing(
        listing_id="1",
        title="Example Laptop",
        normalized_title="example laptop",
        fingerprint="abc",
        url="https://www.facebook.com/marketplace/item/1/",
        price_text=f"${price}",
        price_value=price,
        currency="CAD",
        source_query="laptop",
    )


def test_discount_scores_above_market_median() -> None:
    result = score_listing(
        _listing(75),
        PriceStats(sample_size=8, median_price=100, min_price=90, max_price=120),
    )
    assert result.deal_score == 75.0
    assert result.confidence == 1.0


def test_target_price_adds_bonus() -> None:
    result = score_listing(
        _listing(75),
        PriceStats(sample_size=8, median_price=100),
        watchlist=Watchlist(name="cheap", query="laptop", target_price=80),
    )
    assert result.deal_score == 85.0
