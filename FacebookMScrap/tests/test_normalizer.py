# tests/test_normalizer.py
from facebook_marketplace_scraper.models import RawListing
from facebook_marketplace_scraper.normalizer import normalize_raw_listing, parse_price


def test_parse_price_cad() -> None:
    assert parse_price("C$1,234.50", default_currency="USD") == (1234.5, "CAD")


def test_normalize_listing_from_link_text() -> None:
    listing = normalize_raw_listing(
        RawListing(
            url="/marketplace/item/123456789/?ref=search",
            text="$250\nThinkPad T480\nVictoria, BC",
        ),
        query="thinkpad",
    )
    assert listing.listing_id == "123456789"
    assert listing.title == "ThinkPad T480"
    assert listing.normalized_title == "thinkpad t480"
    assert listing.price_value == 250.0
    assert listing.currency == "CAD"
    assert listing.location == "Victoria, BC"
