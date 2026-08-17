"""National statutory constraints near a coordinate.

The Planning Administration publishes the dynamic TAMA 1 map as a public
ArcGIS service.  This module deliberately asks only for a small, property
relevant subset (flooding, groundwater sensitivity, protected areas, waste,
infrastructure and quarries) and returns source labels rather than attempting
to turn a planning overlay into legal advice.
"""

from __future__ import annotations

from functools import lru_cache

import requests


BASE_URL = (
    "https://ags.iplan.gov.il/arcgis/rest/services/PlanningPublic/"
    "TAMA_1/MapServer"
)

# Match against layer names discovered from the live service.  Layer IDs are
# not hard-coded: the publisher occasionally republishes a map with different
# IDs, while the official Hebrew names remain stable.
KEYWORDS = (
    "הצפה", "רגישות הידרולוגית", "זיהום מי תהום", "שמורה", "גן",
    "יער", "פסולת", "כריה", "חציבה", "תחנת כוח", "גז טבעי",
    "מוביל ארצי", "מסילת ברזל",
)
MAX_LAYERS = 18
TIMEOUT_SECONDS = 12


@lru_cache(maxsize=1)
def relevant_layers() -> tuple[tuple[int, str], ...]:
    """Return the currently published property-relevant TAMA 1 layers."""
    response = requests.get(BASE_URL, params={"f": "json"},
                            timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    data = response.json()
    if data.get("error"):
        raise RuntimeError(data["error"].get("message", "TAMA 1 API error"))
    layers = []
    for layer in data.get("layers", []):
        layer_id = layer.get("id")
        name = str(layer.get("name") or "").strip()
        # Root MapServer metadata does not consistently include geometryType.
        # It does, however, mark groups with subLayerIds; query only leaves.
        if not isinstance(layer_id, int) or layer.get("subLayerIds"):
            continue
        if any(keyword in name for keyword in KEYWORDS):
            layers.append((layer_id, name))
    return tuple(layers[:MAX_LAYERS])


def constraints_at(lat: float, lon: float, limit_per_layer: int = 8) -> dict:
    """Find national planning constraints intersecting one WGS84 point.

    ArcGIS performs the coordinate conversion itself via ``inSR=4326``.  An
    unavailable source raises a normal exception for the caller to report as
    a non-fatal enrichment error; it must never make the parcel view fail.
    """
    hits = []
    checked = []
    params = {
        "f": "json", "where": "1=1", "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint", "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects", "outFields": "*",
        "returnGeometry": "false", "resultRecordCount": str(limit_per_layer),
    }
    for layer_id, layer_name in relevant_layers():
        checked.append(layer_name)
        response = requests.get(f"{BASE_URL}/{layer_id}/query", params=params,
                                timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            continue
        for feature in data.get("features") or []:
            attrs = feature.get("attributes") or {}
            # Preserve the original attributes, but cap the response to simple
            # scalar values so an upstream schema change cannot break JSON.
            clean = {str(k): v for k, v in attrs.items()
                     if isinstance(v, (str, int, float, bool)) or v is None}
            hits.append({"layer": layer_name, "layer_id": layer_id,
                         "attributes": clean})
    return {
        "source": "מינהל התכנון — תמ\"א 1 דינאמית",
        "checked_layers": checked,
        "total": len(hits),
        "hits": hits,
        "note": "שכבת תכנון אינדיקטיבית; יש לאמת מול התשריט והוראות התכנית הרשמיים.",
    }
