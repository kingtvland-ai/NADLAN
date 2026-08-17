# src/facebook_marketplace_scraper/city_resolver.py
from __future__ import annotations

from .israel_cities import ISRAEL_CITY_IDS
from .storage import MarketplaceStore


async def resolve_city_id(store: MarketplaceStore, city_name: str) -> str | None:
    """Look up the real Facebook Marketplace city_id for the given city name.

    Facebook Marketplace search results are anchored geographically by
    city_id (results come from a radius around that location) - the query
    text is only a secondary filter *within* that radius, it does not
    override the location. So every city needs its own correct city_id;
    there is no single fixed anchor that works for all cities (confirmed:
    a fixed Tel Aviv anchor returned 0 results for a Netanya-worded query).

    The static map in israel_cities.py already holds known-good values, so
    this is a plain lookup - no DB-backed verification or browser-based
    discovery is needed. `store` is kept in the signature for backward
    compatibility with existing callers even though it's not used.
    """
    return ISRAEL_CITY_IDS.get(city_name)
