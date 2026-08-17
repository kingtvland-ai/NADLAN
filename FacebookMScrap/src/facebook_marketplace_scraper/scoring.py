# src/facebook_marketplace_scraper/scoring.py
from __future__ import annotations

from .models import MarketplaceListing, PriceStats, ScoredListing, Watchlist
from .valuation import condition_score_adjustment, valuation_profile


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def score_listing(
    listing: MarketplaceListing,
    stats: PriceStats,
    *,
    watchlist: Watchlist | None = None,
) -> ScoredListing:
    price = listing.price_value
    reasons: list[str] = []
    if listing.restricted:
        return ScoredListing(
            listing=listing,
            deal_score=0.0,
            confidence=0.0,
            price_stats=stats,
            reasons=["Listing excluded from scoring by safety classification"],
        )
    if price is None:
        return ScoredListing(
            listing=listing,
            deal_score=0.0,
            confidence=0.0,
            price_stats=stats,
            reasons=["No numeric price available"],
        )

    profile = valuation_profile(listing.category)
    score = 50.0
    if stats.median_price and stats.median_price > 0:
        discount = (stats.median_price - price) / stats.median_price
        score += _clamp(discount * profile.discount_weight, -50.0, 40.0)
        if discount > 0:
            reasons.append(f"{discount:.0%} below {listing.category} comparable median")
        elif discount < 0:
            reasons.append(f"{-discount:.0%} above {listing.category} comparable median")
    else:
        score = 40.0
        reasons.append(f"Limited {listing.category} comparable-price history")

    if stats.previous_price and stats.previous_price > price:
        drop = (stats.previous_price - price) / stats.previous_price
        score += _clamp(drop * profile.price_drop_weight, 0.0, 15.0)
        reasons.append(f"Price dropped {drop:.0%} since previous observation")

    if watchlist and watchlist.target_price is not None and price <= watchlist.target_price:
        score += profile.target_bonus
        reasons.append("At or below watchlist target price")

    condition_adjustment = condition_score_adjustment(listing.condition)
    score += condition_adjustment
    if listing.condition != "unknown":
        reasons.append(f"Condition classified as {listing.condition.replace('_', ' ')}")

    confidence = _clamp(stats.sample_size / max(1, profile.sample_target), 0.1, 1.0)
    if stats.sample_size < 3:
        reasons.append("Low comparable sample size")

    return ScoredListing(
        listing=listing,
        deal_score=round(_clamp(score, 0.0, 100.0), 1),
        confidence=round(confidence, 2),
        price_stats=stats,
        reasons=reasons,
    )
