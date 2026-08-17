# src/facebook_marketplace_scraper/normalizer.py
from __future__ import annotations

import hashlib
import re
from urllib.parse import urljoin, urlparse

from .metadata import infer_metadata
from .models import MarketplaceListing, RawListing

_ITEM_ID_RE = re.compile(r"/marketplace/item/(\d+)")
_PRICE_RE = re.compile(r"(?P<amount>\d[\d,]*(?:\.\d{1,2})?)")
_CURRENCY_MARKERS = (
    ("CA$", "CAD"), ("C$", "CAD"), ("US$", "USD"), ("USD", "USD"), ("CAD", "CAD"),
    ("€", "EUR"), ("£", "GBP"), ("₪", "ILS"), ("ILS", "ILS"),
)
# Symbols/codes that mark a DOM text line as a price line. "$" alone used to be
# enough when this only scraped CAD listings; Israeli Marketplace listings show
# "₪" instead, so that must be checked too or price lines are never detected.
_PRICE_LINE_MARKERS = ("$", "₪", "€", "£", "CAD", "USD", "EUR", "GBP", "ILS")
_FREE_WORDS = {"free", "חינם"}


def extract_listing_id(url: str) -> str:
    match = _ITEM_ID_RE.search(url)
    if match:
        return match.group(1)
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


def normalize_title(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"[^\w\s]", " ", value)
    return " ".join(value.split())


def parse_price(price_text: str | None, *, default_currency: str) -> tuple[float | None, str | None]:
    if not price_text:
        return None, None
    text = price_text.strip()
    if text.casefold() in {"free", "$0", "0", "חינם"}:
        return 0.0, default_currency.upper()
    currency = default_currency.upper()
    for marker, code in _CURRENCY_MARKERS:
        if marker.casefold() in text.casefold():
            currency = code
            break
    match = _PRICE_RE.search(text)
    if not match:
        return None, currency
    try:
        return float(match.group("amount").replace(",", "")), currency
    except ValueError:
        return None, currency


def _split_candidate_text(raw: RawListing) -> tuple[str, str | None, str | None]:
    lines = [line.strip() for line in raw.text.splitlines() if line.strip()]
    price_text = raw.price_text
    if price_text is None:
        for line in lines:
            if line.casefold() in _FREE_WORDS or any(marker in line for marker in _PRICE_LINE_MARKERS):
                price_text = line
                break
    title = None
    for line in lines:
        if line != price_text and not _PRICE_RE.fullmatch(line.replace(",", "")):
            title = line
            break
    if not title and raw.title_hint:
        title = raw.title_hint.strip()
    if not title:
        title = "Untitled Marketplace listing"
    location = raw.location_hint
    if not location and len(lines) >= 3:
        candidates = [line for line in lines if line not in {title, price_text}]
        if candidates:
            location = candidates[-1]
    return title, price_text, location


def normalize_raw_listing(
    raw: RawListing,
    *,
    query: str,
    default_currency: str = "ILS",
) -> MarketplaceListing:
    absolute_url = urljoin("https://www.facebook.com", raw.url)
    parsed = urlparse(absolute_url)
    canonical_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    title, price_text, location = _split_candidate_text(raw)
    price_value, currency = parse_price(price_text, default_currency=default_currency)
    normalized = normalize_title(title)
    fingerprint_source = f"{normalized}|{(location or '').casefold().strip()}"
    fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:24]
    metadata = infer_metadata(
        title=title,
        body=raw.description_hint or raw.text,
        category_hint=raw.category_hint,
        condition_hint=raw.condition_hint,
    )
    return MarketplaceListing(
        listing_id=extract_listing_id(canonical_url),
        title=title,
        normalized_title=normalized,
        fingerprint=fingerprint,
        url=canonical_url,
        price_text=price_text,
        price_value=price_value,
        currency=currency,
        location=location,
        image_url=raw.image_url,
        description=raw.description_hint,
        category=metadata.category,
        condition=metadata.condition,
        classification_source=metadata.source,
        classification_confidence=metadata.confidence,
        restricted=metadata.restricted,
        source_query=query,
    )
