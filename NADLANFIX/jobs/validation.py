"""Data validation layer for NADLANFIX.

Validates listings before they are persisted to the database.
Ensures data quality and catches issues early in the pipeline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ValidationResult:
    """Result of validating a single listing."""
    is_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    normalized: dict | None = None


class ListingValidator:
    """Validates listings against business rules."""

    # Required fields
    REQUIRED_FIELDS = ["listing_id", "source", "external_id", "title", "city"]

    # Price sanity bounds (ILS)
    MIN_PRICE = 10000
    MAX_PRICE = 100_000_000

    # Area sanity bounds (sqm)
    MIN_AREA = 10
    MAX_AREA = 10000

    # Rooms sanity bounds
    MIN_ROOMS = 0.5
    MAX_ROOMS = 30

    def __init__(self):
        self._stats = {
            "total_validated": 0,
            "total_errors": 0,
            "total_warnings": 0,
            "by_source": {},
        }

    def validate(self, listing: dict) -> ValidationResult:
        """Validate a single listing.

        Args:
            listing: Canonical listing dict

        Returns:
            ValidationResult with errors, warnings, and normalized data
        """
        errors = []
        warnings = []
        normalized = dict(listing)

        # Check required fields
        for field_name in self.REQUIRED_FIELDS:
            if not listing.get(field_name):
                errors.append(f"Missing required field: {field_name}")

        # Validate price
        price = listing.get("price")
        if price is not None:
            if price < self.MIN_PRICE:
                warnings.append(f"Price {price} below minimum {self.MIN_PRICE}")
            elif price > self.MAX_PRICE:
                warnings.append(f"Price {price} above maximum {self.MAX_PRICE}")

        # Validate area
        area = listing.get("area_sqm")
        if area is not None:
            if area < self.MIN_AREA:
                warnings.append(f"Area {area} below minimum {self.MIN_AREA}")
            elif area > self.MAX_AREA:
                warnings.append(f"Area {area} above maximum {self.MAX_AREA}")

        # Validate rooms
        rooms = listing.get("rooms")
        if rooms is not None:
            if rooms < self.MIN_ROOMS:
                warnings.append(f"Rooms {rooms} below minimum {self.MIN_ROOMS}")
            elif rooms > self.MAX_ROOMS:
                warnings.append(f"Rooms {rooms} above maximum {self.MAX_ROOMS}")

        # Validate URL format
        url = listing.get("url")
        if url and not url.startswith("http"):
            warnings.append(f"Invalid URL format: {url}")

        # Validate phone format (Israeli)
        phone = listing.get("phone")
        if phone:
            phone_clean = re.sub(r"[^\d]", "", str(phone))
            if len(phone_clean) not in (9, 10):
                warnings.append(f"Phone number length unusual: {phone}")

        # Validate coordinates
        lat = listing.get("lat")
        lon = listing.get("lon")
        if lat is not None and lon is not None:
            if not (-90 <= lat <= 90):
                errors.append(f"Invalid latitude: {lat}")
            if not (-180 <= lon <= 180):
                errors.append(f"Invalid longitude: {lon}")

        # Normalize text fields
        if listing.get("title"):
            normalized["title"] = self._normalize_text(listing["title"])
            normalized["normalized_title"] = self._normalize_text(listing["title"]).lower()

        if listing.get("city"):
            normalized["city"] = self._normalize_text(listing["city"])

        if listing.get("neighborhood"):
            normalized["neighborhood"] = self._normalize_text(listing["neighborhood"])

        # Update stats
        self._stats["total_validated"] += 1
        self._stats["total_errors"] += len(errors)
        self._stats["total_warnings"] += len(warnings)
        source = listing.get("source", "unknown")
        if source not in self._stats["by_source"]:
            self._stats["by_source"][source] = {"validated": 0, "errors": 0, "warnings": 0}
        self._stats["by_source"][source]["validated"] += 1
        self._stats["by_source"][source]["errors"] += len(errors)
        self._stats["by_source"][source]["warnings"] += len(warnings)

        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            normalized=normalized if errors == 0 else None,
        )

    def validate_batch(self, listings: list[dict]) -> tuple[list[dict], list[dict]]:
        """Validate a batch of listings.

        Args:
            listings: List of listing dicts

        Returns:
            Tuple of (valid_listings, invalid_listings)
        """
        valid = []
        invalid = []

        for listing in listings:
            result = self.validate(listing)
            if result.is_valid and result.normalized:
                valid.append(result.normalized)
            else:
                invalid.append({
                    "listing": listing,
                    "errors": result.errors,
                    "warnings": result.warnings,
                })

        return valid, invalid

    def get_stats(self) -> dict:
        """Get validation statistics."""
        return dict(self._stats)

    @staticmethod
    def _normalize_text(text: str) -> str:
        """Normalize text: strip whitespace, collapse spaces."""
        if not text:
            return ""
        return " ".join(text.split())
