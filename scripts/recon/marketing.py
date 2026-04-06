"""
Marketing Scraper — MarketingModule.

Fetches and parses marketing pages, pricing pages, and feature comparison
pages to extract feature lists, pricing tiers, and core vs premium distinctions.

Produces Facts with category=BUSINESS_RULE (pricing) or CONFIGURATION (features).

INV-020: run() MUST NOT raise.
INV-013: All facts have authority=ANECDOTAL.
INV-001: Every Fact has at least one EvidenceRef.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from bs4 import BeautifulSoup

from scripts.models import (
    Authority,
    Confidence,
    EvidenceRef,
    Fact,
    FactCategory,
    SourceType,
)
from scripts.recon.base import (
    ReconError,
    ReconModule,
    ReconModuleStatus,
    ReconProgress,
    ReconRequest,
    ReconResult,
    ReconServices,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; duplicat-rex/0.1; +https://github.com/plwp/duplicat-rex)"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
}

# Back-off schedule for 429 responses (seconds)
_BACKOFF_SCHEDULE = [2, 5, 15, 30]

# Well-known marketing / pricing URL slugs to probe (relative to base URL)
_MARKETING_PROBE_PATHS = [
    "/pricing",
    "/plans",
    "/features",
    "/compare",
    "/comparison",
    "/",
    "/product",
    "/about",
    "/enterprise",
    "/business",
]

# Tier name patterns for pricing extraction
_TIER_NAME_PATTERNS = re.compile(
    r"\b(free|basic|standard|pro|professional|business|enterprise|team|starter|"
    r"plus|premium|advanced|ultimate|personal|individual|growth|scale|unlimited)\b",
    re.IGNORECASE,
)

# Price extraction: $X, $X.XX, $X/month, $X per user
_PRICE_RE = re.compile(
    r"\$\s*(\d+(?:\.\d{1,2})?)\s*(?:/\s*(?:mo(?:nth)?|yr|year|user|seat|month|annually))?",
    re.IGNORECASE,
)

# Feature availability markers in comparison tables
_INCLUDED_MARKERS = {"✓", "✔", "yes", "included", "✅", "●", "•", "unlimited", "true"}
_EXCLUDED_MARKERS = {"✗", "✘", "no", "—", "-", "×", "❌", "○", "false", "n/a"}


# ---------------------------------------------------------------------------
# Internal data containers
# ---------------------------------------------------------------------------


@dataclass
class _PricingTier:
    """A single pricing tier extracted from a pricing page."""

    name: str  # e.g. "Free", "Pro", "Enterprise"
    price_str: str  # Raw price string, e.g. "$10/month"
    price_usd: float | None  # Parsed price in USD (None if free/custom)
    billing_period: str  # "monthly" | "annual" | "one-time" | "custom" | "free"
    features: list[str]  # Feature bullet points listed under this tier
    source_url: str
    raw_excerpt: str


@dataclass
class _MarketingFeature:
    """A feature listed on a marketing or feature-comparison page."""

    name: str  # Feature name/label
    description: str  # Short description or tagline
    tiers: list[str]  # Which pricing tiers include this feature (if known)
    is_premium: bool  # True if requires paid plan
    source_url: str
    raw_excerpt: str
    tags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Module
# ---------------------------------------------------------------------------


class MarketingModule(ReconModule):
    """
    Scrapes marketing pages, pricing pages, and feature comparison pages.

    Strategy:
      1. Use module_config["marketing_urls"] if provided (list of URLs).
      2. Probe well-known marketing/pricing paths relative to the target base URL.
      3. Parse each page for pricing tiers and feature lists.
      4. Emit one Fact per tier (category=BUSINESS_RULE) and one per feature
         (category=CONFIGURATION).
    """

    # --- ReconModule interface ---

    @property
    def name(self) -> str:
        return "marketing"

    @property
    def authority(self) -> Authority:
        return Authority.ANECDOTAL

    @property
    def source_type(self) -> SourceType:
        return SourceType.MARKETING

    @property
    def requires_credentials(self) -> list[str]:
        return []  # Public pages — no credentials needed

    # --- Main entry point ---

    async def run(
        self,
        request: ReconRequest,
        services: ReconServices,
        progress: Callable[[ReconProgress], None] | None = None,
    ) -> ReconResult:
        """
        Execute marketing recon.

        ENSURES: ReconResult.module == "marketing".
        ENSURES: run() does not raise (INV-020).
        """
        started_at = datetime.now(UTC).isoformat()
        t0 = time.monotonic()

        def emit(
            phase: str,
            message: str,
            completed: int | None = None,
            total: int | None = None,
        ) -> None:
            if progress:
                progress(
                    ReconProgress(
                        run_id=request.run_id,
                        module=self.name,
                        phase=phase,
                        message=message,
                        completed=completed,
                        total=total,
                    )
                )

        emit("init", f"Starting marketing recon for {request.target}")

        base_url = request.base_url or f"https://{request.target}"
        facts: list[Fact] = []
        errors: list[ReconError] = []
        urls_visited: list[str] = []

        try:
            # Build HTTP client
            if services.http_client is not None:
                client = services.http_client
                own_client = False
            else:
                client = httpx.AsyncClient(
                    headers=_DEFAULT_HEADERS,
                    follow_redirects=True,
                    timeout=30.0,
                )
                own_client = True

            try:
                # Determine which URLs to scrape
                target_urls = self._discover_urls(base_url, request.module_config)
                emit(
                    "discover",
                    f"Probing {len(target_urls)} marketing page(s)",
                )

                scope_features = (
                    request.scope.feature_keys() if request.scope.resolved_features else []
                )

                for url in target_urls:
                    emit("discover", f"Fetching {url}")
                    html, error = await self._fetch_with_backoff(client, url)
                    if error:
                        errors.append(error)
                        continue

                    urls_visited.append(url)
                    soup = BeautifulSoup(html, "html.parser")

                    # Extract pricing tiers
                    tiers = self._parse_pricing_tiers(soup, url)
                    for tier in tiers:
                        fact = self._tier_to_fact(tier, request.run_id, scope_features)
                        facts.append(fact)

                    # Extract feature listings
                    features = self._parse_marketing_features(soup, url)
                    for feature in features:
                        fact = self._feature_to_fact(feature, request.run_id, scope_features)
                        facts.append(fact)

                    # Stop probing once we have meaningful content
                    if tiers or features:
                        # Still check explicit URLs, but stop probing further paths
                        explicit_urls: list[str] = request.module_config.get("marketing_urls", [])
                        if url not in explicit_urls:
                            break

                emit(
                    "extract",
                    f"Extracted {len(facts)} facts",
                    completed=len(facts),
                    total=len(facts),
                )

            finally:
                if own_client:
                    await client.aclose()

        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error in MarketingModule.run")
            errors.append(
                ReconError(
                    source_url=None,
                    error_type="parse_error",
                    message=f"Unexpected error: {exc}",
                    recoverable=False,
                )
            )

        finished_at = datetime.now(UTC).isoformat()
        duration = time.monotonic() - t0

        if facts:
            status = ReconModuleStatus.PARTIAL if errors else ReconModuleStatus.SUCCESS
        else:
            status = ReconModuleStatus.FAILED

        emit("complete", f"Done: {len(facts)} facts, {len(errors)} errors")

        return ReconResult(
            module=self.name,
            status=status,
            facts=facts,
            errors=errors,
            urls_visited=urls_visited,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
            metrics={
                "facts_found": len(facts),
                "errors": len(errors),
                "urls_visited": len(urls_visited),
            },
        )

    # --- URL discovery ---

    def _discover_urls(self, base_url: str, module_config: dict[str, Any]) -> list[str]:
        """Return ordered list of URLs to try, explicit config first."""
        explicit: list[str] = module_config.get("marketing_urls", [])
        if explicit:
            return explicit

        base = base_url.rstrip("/")
        return [f"{base}{path}" for path in _MARKETING_PROBE_PATHS]

    # --- HTTP fetch with backoff ---

    async def _fetch_with_backoff(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[str, ReconError | None]:
        """Fetch a URL with exponential backoff on 429."""
        for attempt, backoff in enumerate([0] + _BACKOFF_SCHEDULE):
            if backoff:
                await asyncio.sleep(backoff)
            try:
                resp = await client.get(url)
            except httpx.TimeoutException:
                return "", ReconError(
                    source_url=url,
                    error_type="timeout",
                    message=f"Request timed out: {url}",
                    recoverable=True,
                )
            except httpx.RequestError as exc:
                return "", ReconError(
                    source_url=url,
                    error_type="parse_error",
                    message=f"Network error fetching {url}: {exc}",
                    recoverable=False,
                )

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", backoff or 5))
                logger.warning("Rate limited on %s, retrying after %ds", url, retry_after)
                if attempt < len(_BACKOFF_SCHEDULE):
                    await asyncio.sleep(retry_after)
                    continue
                return "", ReconError(
                    source_url=url,
                    error_type="rate_limited",
                    message=f"Rate limited after {attempt + 1} attempts: {url}",
                    recoverable=True,
                )

            if resp.status_code >= 400:
                return "", ReconError(
                    source_url=url,
                    error_type="parse_error",
                    message=f"HTTP {resp.status_code} fetching {url}",
                    recoverable=resp.status_code >= 500,
                )

            return resp.text, None

        return "", ReconError(
            source_url=url,
            error_type="rate_limited",
            message=f"Exhausted retries for {url}",
            recoverable=True,
        )

    # --- Pricing tier parsing ---

    def _parse_pricing_tiers(
        self, soup: BeautifulSoup, page_url: str
    ) -> list[_PricingTier]:
        """
        Extract pricing tiers from the page.

        Heuristics (in priority order):
          1. Pricing cards/columns — elements with tier names + prices.
          2. Pricing tables — tables with tier names in headers and prices in rows.
        """
        tiers: list[_PricingTier] = []
        seen_names: set[str] = set()

        # Remove noise
        for tag in soup.find_all(["nav", "footer", "script", "style"]):
            tag.decompose()

        # --- Heuristic 1: pricing cards ---
        # Look for container elements that mention a tier name and a price
        for el in soup.find_all(["div", "section", "article", "li"]):
            text = el.get_text(" ", strip=True)
            if len(text) < 10 or len(text) > 3000:
                continue

            tier_match = _TIER_NAME_PATTERNS.search(text)
            if not tier_match:
                continue

            # Check this element has a price or "free" / "contact us"
            price_match = _PRICE_RE.search(text)
            is_free = bool(re.search(r"\bfree\b", text, re.I))
            is_custom = bool(re.search(r"\b(contact|custom|quote|talk to sales)\b", text, re.I))

            if not (price_match or is_free or is_custom):
                continue

            tier_name = tier_match.group(0).strip().title()
            if tier_name.lower() in seen_names:
                continue
            seen_names.add(tier_name.lower())

            # Extract price
            price_str = ""
            price_usd: float | None = None
            billing_period = "free" if is_free else ("custom" if is_custom else "monthly")

            if price_match:
                price_str = price_match.group(0)
                try:
                    price_usd = float(price_match.group(1))
                except (ValueError, IndexError):
                    pass
                # Detect annual billing
                if re.search(r"\b(annual|yearly|per year|yr)\b", text, re.I):
                    billing_period = "annual"

            # Extract feature bullets
            features: list[str] = []
            for li in el.find_all("li"):
                li_text = li.get_text(strip=True)
                if li_text and len(li_text) < 200:
                    features.append(li_text)

            tiers.append(
                _PricingTier(
                    name=tier_name,
                    price_str=price_str or ("Free" if is_free else "Custom"),
                    price_usd=price_usd,
                    billing_period=billing_period,
                    features=features[:20],
                    source_url=page_url,
                    raw_excerpt=text[:2000],
                )
            )

        # --- Heuristic 2: pricing tables ---
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True) for th in table.find_all("th")]
            # Check if any header looks like a tier name
            tier_headers = [(i, h) for i, h in enumerate(headers) if _TIER_NAME_PATTERNS.search(h)]
            if not tier_headers:
                continue

            for col_idx, tier_name in tier_headers:
                if tier_name.lower() in seen_names:
                    continue
                seen_names.add(tier_name.lower())

                rows = table.find_all("tr")[1:]  # skip header
                features_in_tier: list[str] = []
                price_str = ""
                price_usd = None
                billing_period = "custom"

                for row in rows:
                    cells = row.find_all(["td", "th"])
                    if not cells:
                        continue
                    row_label = cells[0].get_text(strip=True)

                    if col_idx < len(cells):
                        cell_text = cells[col_idx].get_text(strip=True)
                        # Check if it's a price cell
                        price_m = _PRICE_RE.search(cell_text)
                        if price_m:
                            price_str = price_m.group(0)
                            try:
                                price_usd = float(price_m.group(1))
                            except (ValueError, IndexError):
                                pass
                            billing_period = "monthly"
                        elif cell_text.lower() in _INCLUDED_MARKERS:
                            features_in_tier.append(row_label)
                        elif cell_text.lower() in _EXCLUDED_MARKERS:
                            pass  # Not included in this tier

                is_free = price_usd == 0.0 or "free" in tier_name.lower()
                tiers.append(
                    _PricingTier(
                        name=tier_name.strip().title(),
                        price_str=price_str or ("Free" if is_free else "Custom"),
                        price_usd=price_usd,
                        billing_period="free" if is_free else billing_period,
                        features=features_in_tier[:20],
                        source_url=page_url,
                        raw_excerpt=table.get_text(" ", strip=True)[:2000],
                    )
                )

        return tiers

    # --- Marketing feature parsing ---

    def _parse_marketing_features(
        self, soup: BeautifulSoup, page_url: str
    ) -> list[_MarketingFeature]:
        """
        Extract feature listings from marketing / feature pages.

        Heuristics:
          1. Feature section headings with descriptive paragraphs.
          2. Feature-list <ul> elements with item descriptions.
          3. Feature comparison table rows.
        """
        features: list[_MarketingFeature] = []
        seen_names: set[str] = set()

        # --- Heuristic 1: heading + description pairs ---
        for heading in soup.find_all(["h2", "h3", "h4"]):
            heading_text = heading.get_text(strip=True)
            if not heading_text or len(heading_text) > 100:
                continue

            # Get the next sibling paragraph as the description
            desc = ""
            for sibling in heading.find_next_siblings():
                tag_name = getattr(sibling, "name", None)
                if tag_name in ("h2", "h3", "h4"):
                    break
                if tag_name == "p":
                    desc = sibling.get_text(strip=True)
                    if desc:
                        break

            if not desc:
                continue

            name_key = heading_text.lower()[:60]
            if name_key in seen_names:
                continue
            seen_names.add(name_key)

            is_premium = bool(
                re.search(r"\b(pro|premium|enterprise|paid|upgrade|business)\b", desc, re.I)
            )

            features.append(
                _MarketingFeature(
                    name=heading_text,
                    description=desc,
                    tiers=[],
                    is_premium=is_premium,
                    source_url=page_url,
                    raw_excerpt=f"{heading_text}: {desc}"[:2000],
                )
            )

        # --- Heuristic 2: feature <ul> lists ---
        for ul in soup.find_all("ul"):
            items = ul.find_all("li")
            if len(items) < 2:
                continue

            # Check if items look like feature descriptions (not nav links)
            texts = [li.get_text(strip=True) for li in items if li.get_text(strip=True)]
            # Feature lists typically have items of moderate length
            if not texts or not all(5 < len(t) < 300 for t in texts[:3]):
                continue

            # Find the heading that precedes this list
            heading_el = ul.find_previous(["h1", "h2", "h3", "h4"])
            section_title = heading_el.get_text(strip=True) if heading_el else ""

            for item_text in texts:
                name_key = item_text.lower()[:60]
                if name_key in seen_names:
                    continue
                seen_names.add(name_key)

                is_premium = bool(
                    re.search(r"\b(pro|premium|enterprise|paid|upgrade)\b", item_text, re.I)
                )
                features.append(
                    _MarketingFeature(
                        name=item_text[:100],
                        description=item_text,
                        tiers=[],
                        is_premium=is_premium,
                        source_url=page_url,
                        raw_excerpt=f"{section_title}: {item_text}"[:2000],
                        tags=[section_title[:80]] if section_title else [],
                    )
                )

        return features

    # --- Feature inference ---

    def _infer_feature(
        self, text: str, scope_features: list[str], fallback: str = "marketing"
    ) -> str:
        """Match text against scope features, else derive a slug from text."""
        text_lower = text.lower()
        text_normalised = re.sub(r"[-_]", " ", text_lower)

        for feature in scope_features:
            feature_space = re.sub(r"[-_]", " ", feature.lower())
            feature_hyphen = re.sub(r"[\s_]", "-", feature.lower())
            if feature_space in text_normalised or feature_hyphen in text_lower:
                return feature

        words = re.findall(r"[a-z][a-z0-9]{2,}", text_lower)
        stop_words = {
            "the", "and", "for", "with", "that", "this", "has", "our", "your",
            "more", "now", "get", "all", "any", "can", "you", "one", "per",
        }
        for word in words:
            if word not in stop_words:
                return re.sub(r"[^a-z0-9-]", "-", word).strip("-") or fallback

        return fallback

    # --- Fact creation ---

    def _tier_to_fact(
        self,
        tier: _PricingTier,
        run_id: str,
        scope_features: list[str],
    ) -> Fact:
        """Convert a _PricingTier to a Fact with category=BUSINESS_RULE."""
        feature = self._infer_feature(tier.name + " pricing", scope_features, "pricing")

        billing_desc = (
            f"at {tier.price_str}/{tier.billing_period}"
            if tier.price_str not in ("Free", "Custom")
            else tier.price_str.lower()
        )
        features_summary = (
            f" including: {', '.join(tier.features[:5])}" if tier.features else ""
        )
        claim = (
            f"The '{tier.name}' pricing tier is offered {billing_desc}"
            f"{features_summary}."
        )

        evidence = EvidenceRef(
            source_url=tier.source_url,
            locator=f"pricing tier: {tier.name}",
            source_title=f"{tier.name} plan",
            raw_excerpt=tier.raw_excerpt[:2000] if tier.raw_excerpt else None,
        )

        structured_data: dict[str, Any] = {
            "tier_name": tier.name,
            "price_str": tier.price_str,
            "price_usd": tier.price_usd,
            "billing_period": tier.billing_period,
            "features": tier.features,
            "source_url": tier.source_url,
        }

        return Fact(
            feature=feature,
            category=FactCategory.BUSINESS_RULE,
            claim=claim,
            evidence=[evidence],
            source_type=self.source_type,
            structured_data=structured_data,
            module_name=self.name,
            authority=self.authority,
            confidence=Confidence.LOW,
            run_id=run_id,
        )

    def _feature_to_fact(
        self,
        feature: _MarketingFeature,
        run_id: str,
        scope_features: list[str],
    ) -> Fact:
        """Convert a _MarketingFeature to a Fact with category=CONFIGURATION."""
        inferred_feature = self._infer_feature(
            feature.name + " " + feature.description, scope_features, "features"
        )

        premium_clause = " (requires paid plan)" if feature.is_premium else ""
        desc_preview = feature.description[:200].rstrip()
        claim = f"Marketing page describes feature '{feature.name}'{premium_clause}: {desc_preview}"

        evidence = EvidenceRef(
            source_url=feature.source_url,
            locator=f"feature: {feature.name[:100]}",
            source_title=feature.name[:200],
            raw_excerpt=feature.raw_excerpt[:2000] if feature.raw_excerpt else None,
        )

        structured_data: dict[str, Any] = {
            "feature_name": feature.name,
            "description": feature.description,
            "tiers": feature.tiers,
            "is_premium": feature.is_premium,
            "tags": feature.tags,
            "source_url": feature.source_url,
        }

        return Fact(
            feature=inferred_feature,
            category=FactCategory.CONFIGURATION,
            claim=claim,
            evidence=[evidence],
            source_type=self.source_type,
            structured_data=structured_data,
            module_name=self.name,
            authority=self.authority,
            confidence=Confidence.LOW,
            run_id=run_id,
        )
