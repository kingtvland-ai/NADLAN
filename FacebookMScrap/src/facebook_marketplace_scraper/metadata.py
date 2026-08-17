# src/facebook_marketplace_scraper/metadata.py
from __future__ import annotations

import re
from dataclasses import dataclass

from .models import ClassificationResult


@dataclass(frozen=True)
class Rule:
    category: str
    patterns: tuple[re.Pattern[str], ...]


def _rx(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


_CATEGORY_RULES = (
    Rule("real_estate", _rx(
        r"\bapartment\b", r"\bcondo\b", r"\bfor rent\b", r"\bfor sale\b",
        r"\breal estate\b", r"\bproperty\b", r"\bstudio\b", r"\bbedroom\b",
        r"\bsqft\b", r"\bsq ft\b", r"\bhouse\b",
        r"דירה", r"להשכרה", r"למכירה", r"נכס", r"חדרים", r"נדל\"?ן", r"וילה", r"פנטהאוז",
    )),
    Rule("computers", _rx(r"\bthinkpad\b", r"\blaptop\b", r"\bdesktop\b", r"\bmacbook\b", r"\bcomputer\b")),
    Rule("solar", _rx(r"\bsolar\b", r"\binverter\b", r"\bcharge controller\b", r"\bpv\b")),
    Rule("electronics", _rx(r"\btelevision\b", r"\btv\b", r"\bmonitor\b", r"\bcamera\b", r"\bspeaker\b")),
    Rule("tools", _rx(r"\bdrill\b", r"\bsaw\b", r"\btool\b", r"\bcompressor\b")),
    Rule("automotive", _rx(r"\bvehicle\b", r"\bcar\b", r"\btruck\b", r"\btire\b", r"\bwheel\b")),
    Rule("bicycles", _rx(r"\bbicycle\b", r"\bbike\b", r"\bmountain bike\b")),
    Rule("furniture", _rx(r"\bsofa\b", r"\bcouch\b", r"\bdesk\b", r"\btable\b", r"\bchair\b")),
    Rule("appliances", _rx(r"\bfridge\b", r"\brefrigerator\b", r"\bfreezer\b", r"\bwasher\b", r"\bdryer\b")),
)

_CONDITION_RULES: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    ("parts", _rx(r"\bfor parts\b", r"\bnot working\b", r"\bbroken\b", r"\bas is\b")),
    ("new", _rx(r"\bbrand new\b", r"\bnew in box\b", r"\bunopened\b")),
    ("like_new", _rx(r"\blike new\b", r"\bmint\b", r"\bexcellent condition\b")),
    ("fair", _rx(r"\bfair condition\b", r"\bwell used\b", r"\bwear and tear\b")),
    ("good", _rx(r"\bgood condition\b", r"\bworks great\b", r"\bworking well\b")),
)


def infer_metadata(
    *,
    title: str,
    body: str = "",
    category_hint: str | None = None,
    condition_hint: str | None = None,
) -> ClassificationResult:
    """Cheap deterministic metadata classification used before optional local-LLM refinement."""
    text = f"{title}\n{body}".strip()

    category = category_hint.casefold().strip() if category_hint else "other"
    category_confidence = 0.45 if category != "other" else 0.2
    if category == "other":
        for rule in _CATEGORY_RULES:
            if any(pattern.search(text) for pattern in rule.patterns):
                category = rule.category
                category_confidence = 0.78
                break

    condition = condition_hint.casefold().strip() if condition_hint else "unknown"
    condition_confidence = 0.45 if condition != "unknown" else 0.0
    if condition == "unknown":
        for name, patterns in _CONDITION_RULES:
            if any(pattern.search(text) for pattern in patterns):
                condition = name
                condition_confidence = 0.82
                break

    confidence = max(category_confidence, condition_confidence)
    return ClassificationResult(
        category=category,
        condition=condition,
        confidence=confidence,
        restricted=False,
        source="heuristic",
    )
