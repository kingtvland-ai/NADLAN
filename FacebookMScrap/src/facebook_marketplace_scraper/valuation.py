# src/facebook_marketplace_scraper/valuation.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ValuationProfile:
    comparable_threshold: float
    sample_target: int
    discount_weight: float = 100.0
    price_drop_weight: float = 50.0
    target_bonus: float = 10.0


_DEFAULT = ValuationProfile(0.60, 8)
_PROFILES: dict[str, ValuationProfile] = {
    "computers": ValuationProfile(0.68, 6, 105.0, 55.0, 10.0),
    "electronics": ValuationProfile(0.65, 7, 100.0, 50.0, 10.0),
    "solar": ValuationProfile(0.70, 5, 105.0, 55.0, 10.0),
    "tools": ValuationProfile(0.62, 6, 95.0, 50.0, 10.0),
    "automotive": ValuationProfile(0.76, 5, 90.0, 45.0, 8.0),
    "bicycles": ValuationProfile(0.67, 6, 95.0, 50.0, 10.0),
    "furniture": ValuationProfile(0.55, 8, 85.0, 45.0, 10.0),
    "appliances": ValuationProfile(0.60, 7, 90.0, 50.0, 10.0),
}

_CONDITION_ADJUSTMENTS = {
    "new": 3.0,
    "like_new": 2.0,
    "good": 0.0,
    "unknown": 0.0,
    "fair": -5.0,
    "parts": -18.0,
}


def valuation_profile(category: str) -> ValuationProfile:
    return _PROFILES.get(category, _DEFAULT)


def condition_score_adjustment(condition: str) -> float:
    return _CONDITION_ADJUSTMENTS.get(condition, -1.0)
