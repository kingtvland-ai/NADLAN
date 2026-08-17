# tests/test_comparables.py
from facebook_marketplace_scraper.comparables import (
    comparable_anchor,
    comparison_tokens,
    title_similarity,
)


def test_comparable_tokens_normalize_units_and_noise() -> None:
    assert comparison_tokens("Brand New Solar Panels 100 Watts") == ("solar", "panel", "100w")
    assert comparison_tokens("ThinkPad T480 16 GB - excellent condition") == (
        "thinkpad",
        "t480",
        "16gb",
    )


def test_similar_model_titles_group_together() -> None:
    similarity = title_similarity("Lenovo ThinkPad T480 i5 16GB", "ThinkPad T480 laptop")
    assert similarity >= 0.6
    assert comparable_anchor("Lenovo ThinkPad T480 i5 16GB") == "thinkpad"


def test_different_model_numbers_do_not_group() -> None:
    assert title_similarity("ThinkPad T480 laptop", "ThinkPad T490 laptop") == 0.0


def test_unit_variants_group() -> None:
    assert title_similarity("100 watt solar panel", "Solar panels 100W") >= 0.9
