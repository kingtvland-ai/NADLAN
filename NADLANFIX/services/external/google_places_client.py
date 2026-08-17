"""Live Google Places enrichment for authorised business/professional lookup.

This adapter deliberately returns live results and does not mirror Google's
place data into the local database.  It is for finding businesses such as
appraisers, surveyors, architects and contractors near a locality, never for
finding private individuals.

Set ``GOOGLE_MAPS_API_KEY`` outside the repository. Enable the current Places
API in the Google Cloud project and restrict the key to this service.
"""

from __future__ import annotations

import os

import requests


URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join((
    "places.id", "places.displayName", "places.formattedAddress",
    "places.location", "places.nationalPhoneNumber", "places.websiteUri",
    "places.googleMapsUri", "places.businessStatus", "places.types",
))
TIMEOUT_SECONDS = 20

# The dashboard exposes only these business searches.  Keeping the choices
# closed prevents this integration from becoming a people-search endpoint.
PROFESSION_CATEGORIES = {
    "appraiser": "שמאי מקרקעין",
    "surveyor": "מודד מוסמך",
    "architect": "אדריכל",
    "contractor": "קבלן בניין",
}


class GooglePlacesError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(os.environ.get("GOOGLE_MAPS_API_KEY"))


def search_text(text_query: str, *, api_key: str | None = None,
                language_code: str = "he", region_code: str = "IL",
                page_size: int = 20) -> dict:
    """Search public business places using the official Places API (New)."""
    key = api_key or os.environ.get("GOOGLE_MAPS_API_KEY")
    if not key:
        raise GooglePlacesError("GOOGLE_MAPS_API_KEY is not configured")
    query = " ".join((text_query or "").split())
    if len(query) < 2:
        raise ValueError("text_query must contain at least two characters")
    if not 1 <= page_size <= 20:
        raise ValueError("page_size must be between 1 and 20")
    response = requests.post(
        URL,
        headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": FIELD_MASK,
                 "Content-Type": "application/json"},
        json={"textQuery": query, "languageCode": language_code,
              "regionCode": region_code, "pageSize": page_size},
        timeout=TIMEOUT_SECONDS,
    )
    try:
        data = response.json()
    except ValueError as exc:
        raise GooglePlacesError("Google Places returned a non-JSON response") from exc
    if not response.ok:
        message = data.get("error", {}).get("message", response.reason)
        raise GooglePlacesError(f"Google Places request failed: {message}")
    places = []
    for item in data.get("places") or []:
        name = item.get("displayName") or {}
        location = item.get("location") or {}
        places.append({
            "place_id": item.get("id"),
            "name": name.get("text") if isinstance(name, dict) else name,
            "address": item.get("formattedAddress"),
            "lat": location.get("latitude"), "lon": location.get("longitude"),
            "phone": item.get("nationalPhoneNumber"),
            "website": item.get("websiteUri"),
            "google_maps": item.get("googleMapsUri"),
            "status": item.get("businessStatus"), "types": item.get("types") or [],
        })
    return {"query": query, "source": "Google Places API", "total": len(places),
            "places": places, "next_page_token": data.get("nextPageToken")}


def professionals_in(locality: str, profession: str = "שמאי מקרקעין", **kwargs) -> dict:
    """Convenience lookup for one professional category near an Israeli locality."""
    locality = " ".join((locality or "").split())
    if not locality:
        raise ValueError("locality is required")
    return search_text(f"{profession} ב{locality}, ישראל", **kwargs)


def professionals_by_category(locality: str, category: str = "appraiser", **kwargs) -> dict:
    """Search one approved professional category near a locality."""
    category = (category or "").strip().lower()
    profession = PROFESSION_CATEGORIES.get(category)
    if not profession:
        raise ValueError("unknown professional category")
    return professionals_in(locality, profession, **kwargs)
