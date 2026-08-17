"""Deal scoring for Facebook Marketplace listings.

Ported from FacebookMScrap's scoring.py. Computes a 0-100 deal score based on
price comparison with similar listings, price drops, target price hits, and
condition adjustments.
"""

from __future__ import annotations
from ingestion.normalization.facebook_valuation import condition_score_adjustment, valuation_profile


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def score_listing(
    listing: dict,
    stats: dict,
    *,
    watchlist: dict | None = None,
) -> dict:
    """Compute a deal score for a listing.

    Args:
        listing: Dict with keys: price_value, category, condition, restricted
        stats: Dict with keys: median_price, sample_size, previous_price
        watchlist: Optional dict with key: target_price

    Returns:
        Dict with keys: deal_score, confidence, reasons
    """
    price = listing.get("price_value")
    reasons: list[str] = []

    if listing.get("restricted"):
        return {
            "deal_score": 0.0,
            "confidence": 0.0,
            "reasons": ["Listing excluded from scoring by safety classification"],
        }
    if price is None:
        return {
            "deal_score": 0.0,
            "confidence": 0.0,
            "reasons": ["No numeric price available"],
        }

    category = listing.get("category", "other")
    profile = valuation_profile(category)
    score = 50.0

    median_price = stats.get("median_price")
    if median_price and median_price > 0:
        discount = (median_price - price) / median_price
        score += _clamp(discount * profile.discount_weight, -50.0, 40.0)
        if discount > 0:
            reasons.append(f"{discount:.0%} below {category} comparable median")
        elif discount < 0:
            reasons.append(f"{-discount:.0%} above {category} comparable median")
    else:
        score = 40.0
        reasons.append(f"Limited {category} comparable-price history")

    previous_price = stats.get("previous_price")
    if previous_price and previous_price > price:
        drop = (previous_price - price) / previous_price
        score += _clamp(drop * profile.price_drop_weight, 0.0, 15.0)
        reasons.append(f"Price dropped {drop:.0%} since previous observation")

    if watchlist and watchlist.get("target_price") is not None and price <= watchlist["target_price"]:
        score += profile.target_bonus
        reasons.append("At or below watchlist target price")

    condition = listing.get("condition", "unknown")
    condition_adjustment = condition_score_adjustment(condition)
    score += condition_adjustment
    if condition != "unknown":
        reasons.append(f"Condition classified as {condition.replace('_', ' ')}")

    sample_size = stats.get("sample_size", 0)
    confidence = _clamp(sample_size / max(1, profile.sample_target), 0.1, 1.0)
    if sample_size < 3:
        reasons.append("Low comparable sample size")

    return {
        "deal_score": round(_clamp(score, 0.0, 100.0), 1),
        "confidence": round(confidence, 2),
        "reasons": reasons,
    }
