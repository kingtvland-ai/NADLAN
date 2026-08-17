# src/facebook_marketplace_scraper/llm_classifier.py
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from time import monotonic

import httpx

from .models import ClassificationResult, MarketplaceListing

logger = logging.getLogger(__name__)

_ALLOWED_CATEGORIES = {
    "computers", "electronics", "solar", "tools", "automotive", "bicycles", "furniture",
    "appliances", "other", "restricted",
}
_ALLOWED_CONDITIONS = {"new", "like_new", "good", "fair", "parts", "unknown"}


@dataclass(frozen=True)
class LlamaClassifierSettings:
    enabled: bool = False
    base_url: str = "http://127.0.0.1:8080/v1"
    model: str = "local"
    timeout_seconds: float = 20.0
    uncertain_only: bool = True

    @classmethod
    def from_env(cls) -> LlamaClassifierSettings:
        enabled = os.getenv("FBMS_LLM_ENABLED", "0").casefold() in {"1", "true", "yes", "on"}
        return cls(
            enabled=enabled,
            base_url=os.getenv("FBMS_LLM_URL", "http://127.0.0.1:8080/v1").rstrip("/"),
            model=os.getenv("FBMS_LLM_MODEL", "local"),
            timeout_seconds=float(os.getenv("FBMS_LLM_TIMEOUT", "20")),
            uncertain_only=os.getenv("FBMS_LLM_UNCERTAIN_ONLY", "1").casefold()
            not in {"0", "false", "no", "off"},
        )


class LocalLlamaClassifier:
    """Optional low-concurrency metadata refinement through a local llama-server."""

    def __init__(self, settings: LlamaClassifierSettings, *, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client or httpx.AsyncClient(timeout=settings.timeout_seconds)
        self._owns_client = client is None
        self._semaphore = asyncio.Semaphore(1)
        self._unavailable_until = 0.0

    async def __aenter__(self) -> LocalLlamaClassifier:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def health(self) -> bool:
        if not self.settings.enabled:
            return False
        try:
            response = await self._client.get(f"{self.settings.base_url}/health")
            healthy = response.status_code == 200
            if not healthy:
                self._unavailable_until = monotonic() + 60.0
            return healthy
        except httpx.HTTPError:
            self._unavailable_until = monotonic() + 60.0
            return False

    async def classify(self, listing: MarketplaceListing) -> ClassificationResult | None:
        if not self.settings.enabled or monotonic() < self._unavailable_until:
            return None
        if self.settings.uncertain_only and (
            listing.category != "other" and listing.condition != "unknown"
            and listing.classification_confidence >= 0.75
        ):
            return None

        prompt = (
            "Classify this Marketplace listing for local valuation. Return JSON only with keys "
            "category, condition, confidence, restricted. category must be one of: "
            "computers, electronics, solar, tools, automotive, bicycles, furniture, appliances, "
            "other, restricted. Mark restricted=true for age-regulated or dangerous goods. "
            "condition must be new, like_new, good, fair, parts, or unknown.\n\n"
            f"Title: {listing.title[:500]}\n"
            f"Description: {(listing.description or '')[:1200]}\n"
            f"Price: {listing.price_text or 'unknown'}\n"
            f"Location: {listing.location or 'unknown'}"
        )
        payload = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": "You are a conservative product metadata classifier."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": 160,
        }
        try:
            async with self._semaphore:
                response = await self._client.post(
                    f"{self.settings.base_url}/chat/completions",
                    json=payload,
                )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            result = parse_classifier_json(str(content))
            return result.model_copy(update={"source": "llama.cpp"})
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            self._unavailable_until = monotonic() + 60.0
            logger.warning("local LLM classification failed; keeping heuristic metadata: %s", exc)
            return None


def parse_classifier_json(content: str) -> ClassificationResult:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:].lstrip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("classifier did not return a JSON object")
    data = json.loads(text[start : end + 1])
    category = str(data.get("category", "other")).casefold().strip()
    condition = str(data.get("condition", "unknown")).casefold().strip()
    if category not in _ALLOWED_CATEGORIES:
        category = "other"
    if condition not in _ALLOWED_CONDITIONS:
        condition = "unknown"
    confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    restricted = bool(data.get("restricted", False)) or category == "restricted"
    return ClassificationResult(
        category=category,
        condition=condition,
        confidence=confidence,
        restricted=restricted,
        source="llama.cpp",
    )
