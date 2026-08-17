# src/facebook_marketplace_scraper/comparables.py
from __future__ import annotations

import re

_STOPWORDS = {
    "a",
    "an",
    "and",
    "brand",
    "condition",
    "excellent",
    "firm",
    "for",
    "good",
    "great",
    "like",
    "mint",
    "new",
    "obo",
    "only",
    "pickup",
    "sale",
    "selling",
    "the",
    "used",
    "with",
}

_UNIT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:gigabytes?|gbs?)\b"), r"\1gb"),
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:terabytes?|tbs?)\b"), r"\1tb"),
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:watts?|w)\b"), r"\1w"),
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:kilowatts?|kw)\b"), r"\1kw"),
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:volts?|v)\b"), r"\1v"),
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:amp[ -]?hours?|ah)\b"), r"\1ah"),
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:inches?|inch|in)\b"), r"\1in"),
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
MIN_COMPARABLE_SIMILARITY = 0.60


def comparison_tokens(title: str) -> tuple[str, ...]:
    """Return stable, low-noise tokens for comparable-title matching."""
    value = title.casefold()
    for pattern, replacement in _UNIT_PATTERNS:
        value = pattern.sub(replacement, value)

    tokens: list[str] = []
    for token in _TOKEN_RE.findall(value):
        if token in _STOPWORDS:
            continue
        if (
            token.endswith("s")
            and not token.endswith(("ss", "us", "is"))
            and len(token) > 4
            and not any(char.isdigit() for char in token)
        ):
            token = token[:-1]
        if token and token not in tokens:
            tokens.append(token)
    return tuple(tokens)


def comparable_anchor(title: str) -> str | None:
    """Pick a broad SQL prefilter token before the stricter similarity check."""
    tokens = comparison_tokens(title)
    alphabetic = [token for token in tokens if token.isalpha() and len(token) >= 4]
    if alphabetic:
        return max(alphabetic[:4], key=len)
    return tokens[0] if tokens else None


def title_similarity(left: str, right: str) -> float:
    """Weighted token similarity in [0, 1], emphasizing model/spec tokens."""
    left_tokens = set(comparison_tokens(left))
    right_tokens = set(comparison_tokens(right))
    if not left_tokens or not right_tokens:
        return 0.0

    left_models = {token for token in left_tokens if any(char.isdigit() for char in token)}
    right_models = {token for token in right_tokens if any(char.isdigit() for char in token)}
    if left_models and right_models and left_models.isdisjoint(right_models):
        return 0.0

    def weight(token: str) -> float:
        return 2.5 if any(char.isdigit() for char in token) else 1.0

    intersection = left_tokens & right_tokens
    union = left_tokens | right_tokens
    shared_weight = sum(weight(token) for token in intersection)
    union_weight = sum(weight(token) for token in union)
    left_weight = sum(weight(token) for token in left_tokens)
    right_weight = sum(weight(token) for token in right_tokens)

    jaccard = shared_weight / union_weight if union_weight else 0.0
    containment = shared_weight / min(left_weight, right_weight)
    score = (0.60 * jaccard) + (0.40 * containment)
    if left_models and right_models and left_models & right_models:
        score += 0.15
    return round(min(1.0, score), 4)
