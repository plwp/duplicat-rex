"""
Help Center Scraper — HelpCenterModule.

Scrapes user-facing help documentation to extract feature descriptions,
relationships, and the user mental model of the target product.

Crawls from sitemap.xml or index page, follows same-domain links.
Produces Facts with category=USER_FLOW or UI_COMPONENT.

INV-020: run() MUST NOT raise.
INV-013: All facts have authority=OBSERVATIONAL.
INV-001: Every Fact has at least one EvidenceRef.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

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
    "Accept": "text/html,application/xhtml+xml,application/xml,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
}

# Back-off schedule for 429 responses (seconds)
_BACKOFF_SCHEDULE = [2, 5, 15, 30]

# Max help pages to crawl (safety valve)
_MAX_PAGES = 150

# Sitemap paths to probe (relative to base URL)
_SITEMAP_PROBE_PATHS = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap/",
    "/sitemap",
    "/help/sitemap.xml",
    "/support/sitemap.xml",
]

# Help center index paths to probe when no sitemap found
_HELP_INDEX_PROBE_PATHS = [
    "/help",
    "/support",
    "/help-center",
    "/knowledge-base",
    "/docs",
    "/faq",
    "/en/support",
    "/hc/en-us",
]

# Minimum content length to consider a page worth parsing (chars)
_MIN_CONTENT_LENGTH = 100

# XML sitemap namespace
_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"

# Keywords that indicate a user-flow article (multi-step process)
_FLOW_KEYWORDS = re.compile(
    r"\b(how to|steps?|getting started|guide|tutorial|walkthrough|set up|create a|add a|"
    r"move|copy|archive|invite|share|export|import|configure|enable|disable|manage)\b",
    re.I,
)

# Keywords that indicate a UI component description
_UI_KEYWORDS = re.compile(
    r"\b(button|menu|sidebar|panel|modal|dialog|card|board|list|column|dropdown|"
    r"toolbar|icon|badge|label|tag|filter|search|view|workspace|dashboard)\b",
    re.I,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _article_category(title: str, body_text: str) -> FactCategory:
    """
    Classify a help article as USER_FLOW or UI_COMPONENT based on content.

    Prefers USER_FLOW for how-to articles; falls back to UI_COMPONENT for
    feature/component descriptions; defaults to USER_FLOW.
    """
    combined = f"{title} {body_text[:500]}"
    if _FLOW_KEYWORDS.search(combined):
        return FactCategory.USER_FLOW
    if _UI_KEYWORDS.search(combined):
        return FactCategory.UI_COMPONENT
    return FactCategory.USER_FLOW


def _extract_article_text(soup: BeautifulSoup) -> str:
    """
    Extract the main article body text from a help page.

    Tries common help-center content containers in priority order.
    Falls back to <body> text if nothing specific is found.
    """
    # Common help-center content containers
    selectors = [
        "article",
        '[class*="article"]',
        '[class*="content"]',
        '[class*="help"]',
        "main",
        ".post-body",
        "#article-body",
    ]
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            text = el.get_text(" ", strip=True)
            if len(text) >= _MIN_CONTENT_LENGTH:
                return text
    return soup.get_text(" ", strip=True)


def _extract_related_features(text: str) -> list[str]:
    """
    Extract feature names mentioned in a help article.

    Looks for capitalized nouns that appear to be product features.
    Returns up to 5 candidates.
    """
    # Match product-feature-like phrases: capitalized words (possibly multi-word)
    matches = re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b", text)
    seen: set[str] = set()
    features: list[str] = []
    for m in matches:
        slug = m.lower().replace(" ", "-")
        if slug not in seen and len(slug) > 2:
            seen.add(slug)
            features.append(slug)
        if len(features) >= 5:
            break
    return features


def _infer_feature_from_url_and_title(url: str, title: str, scope_features: list[str]) -> str:
    """
    Infer the feature key from the URL path and page title.

    Priority:
      1. Exact match of a scope feature in URL path or title.
      2. First meaningful URL path segment.
      3. First word of the title (lowercased, slugified).
      4. Default "help-center".
    """
    parsed = urlparse(url)
    path_segments = [s for s in parsed.path.strip("/").split("/") if s]

    # Try scope features against URL and title
    title_lower = title.lower()
    for feature in scope_features:
        slug = feature.lower()
        if slug in title_lower or any(slug in seg.lower() for seg in path_segments):
            return feature

    # Skip generic path prefixes
    _skip = re.compile(r"^(help|support|hc|en|us|articles?|docs?|kb|faq|\d+)$", re.I)
    for seg in path_segments:
        if not _skip.match(seg):
            return re.sub(r"[^a-z0-9-]", "-", seg.lower()).strip("-") or "help-center"

    # Fall back to title slug
    if title:
        first_word = title.split()[0] if title.split() else ""
        slug = re.sub(r"[^a-z0-9-]", "-", first_word.lower()).strip("-")
        if slug:
            return slug

    return "help-center"


# ---------------------------------------------------------------------------
# Module
# ---------------------------------------------------------------------------


class HelpCenterModule(ReconModule):
    """
    Scrapes user-facing help documentation.

    Strategy:
      1. Probe for sitemap.xml — extract all help article URLs.
      2. Fall back to crawling the help index page if no sitemap found.
      3. For each page: extract article text, classify it, build a Fact.

    Produces Facts with:
      - category = USER_FLOW (how-to, guides) or UI_COMPONENT (feature descriptions)
      - authority = OBSERVATIONAL
      - source_type = HELP_CENTER
    """

    @property
    def name(self) -> str:
        return "help_center"

    @property
    def authority(self) -> Authority:
        return Authority.OBSERVATIONAL

    @property
    def source_type(self) -> SourceType:
        return SourceType.HELP_CENTER

    @property
    def requires_credentials(self) -> list[str]:
        return []

    # --- Main entry point ---

    async def run(
        self,
        request: ReconRequest,
        services: ReconServices,
        progress: Callable[[ReconProgress], None] | None = None,
    ) -> ReconResult:
        """
        Execute help center recon.

        ENSURES: ReconResult.module == "help_center".
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

        emit("init", f"Starting help center recon for {request.target}")

        base_url = request.base_url or f"https://{request.target}"
        facts: list[Fact] = []
        errors: list[ReconError] = []
        urls_visited: list[str] = []

        try:
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
                scope_features = (
                    request.scope.feature_keys() if request.scope.resolved_features else []
                )

                # Step 1: discover article URLs from sitemap or index
                emit("discover", "Probing for sitemap.xml")
                article_urls = await self._discover_urls(
                    client, base_url, request.module_config, request.budgets, emit
                )
                emit(
                    "discover",
                    f"Found {len(article_urls)} candidate help article URLs",
                    completed=len(article_urls),
                    total=len(article_urls),
                )

                # Step 2: scrape each article
                max_pages = request.budgets.get("max_pages", _MAX_PAGES)
                capped = article_urls[:max_pages]
                for i, url in enumerate(capped):
                    emit("extract", f"Scraping {url}", completed=i + 1, total=len(capped))
                    html, error = await self._fetch_with_backoff(client, url)
                    if error:
                        errors.append(error)
                        continue
                    urls_visited.append(url)
                    fact = self._page_to_fact(html, url, request.run_id, scope_features)
                    if fact is not None:
                        facts.append(fact)

            finally:
                if own_client:
                    await client.aclose()

        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error in HelpCenterModule.run")
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
                "articles_found": len(facts),
                "errors": len(errors),
                "urls_visited": len(urls_visited),
            },
        )

    # --- URL Discovery ---

    async def _discover_urls(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        module_config: dict[str, Any],
        budgets: dict[str, int],
        emit: Callable[[str, str, int | None, int | None], None],
    ) -> list[str]:
        """
        Discover help article URLs via sitemap.xml or crawl.

        Returns a deduplicated list of same-domain article URLs.
        """
        # Allow caller to specify explicit help URL
        explicit_help_url: str | None = module_config.get("help_url")
        explicit_sitemap_url: str | None = module_config.get("sitemap_url")

        domain = urlparse(base_url).netloc

        # Try explicit sitemap first
        if explicit_sitemap_url:
            urls = await self._fetch_sitemap(client, explicit_sitemap_url, domain)
            if urls:
                return urls

        # Probe well-known sitemap paths
        for path in _SITEMAP_PROBE_PATHS:
            sitemap_url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
            urls = await self._fetch_sitemap(client, sitemap_url, domain)
            if urls:
                return urls

        # Fall back to crawling help index page
        start_url = explicit_help_url or self._probe_help_index(base_url)
        emit("discover", f"No sitemap found, crawling index at {start_url}")
        urls, crawl_errors = await self._crawl_index(client, start_url, domain, budgets)
        return urls

    def _probe_help_index(self, base_url: str) -> str:
        """Return the most likely help center index URL for this domain."""
        # Default: use the first probe path
        return urljoin(base_url.rstrip("/") + "/", _HELP_INDEX_PROBE_PATHS[0].lstrip("/"))

    async def _fetch_sitemap(
        self, client: httpx.AsyncClient, url: str, domain: str
    ) -> list[str]:
        """
        Fetch and parse a sitemap.xml.

        Returns a list of same-domain article URLs (empty if not found/parseable).
        Handles sitemap index files by recursively fetching sub-sitemaps.
        """
        try:
            resp = await client.get(url)
            if resp.status_code != 200:
                return []
            content = resp.text
        except (httpx.RequestError, httpx.TimeoutException):
            return []

        try:
            root = ElementTree.fromstring(content)
        except ElementTree.ParseError:
            return []

        tag = root.tag.lower()
        urls: list[str] = []

        # Sitemap index — recurse into sub-sitemaps
        if "sitemapindex" in tag:
            loc_tag = f"{{{_SITEMAP_NS}}}loc" if _SITEMAP_NS in root.tag else "loc"
            sitemap_tag = f"{{{_SITEMAP_NS}}}sitemap" if _SITEMAP_NS in root.tag else "sitemap"
            for sitemap_el in root.iter(sitemap_tag):
                loc_el = sitemap_el.find(loc_tag)
                if loc_el is not None and loc_el.text:
                    sub_urls = await self._fetch_sitemap(client, loc_el.text.strip(), domain)
                    urls.extend(sub_urls)

        # Regular sitemap — extract <loc> entries
        else:
            ns_prefix = f"{{{_SITEMAP_NS}}}" if _SITEMAP_NS in (root.tag or "") else ""
            loc_tag = f"{ns_prefix}loc"
            for url_el in root.iter(loc_tag):
                if url_el.text:
                    loc = url_el.text.strip()
                    if urlparse(loc).netloc == domain:
                        urls.append(loc)

        return urls

    async def _crawl_index(
        self,
        client: httpx.AsyncClient,
        start_url: str,
        domain: str,
        budgets: dict[str, int],
    ) -> tuple[list[str], list[ReconError]]:
        """
        Crawl a help center index page and collect article links.

        Follows same-domain links up to max_pages budget.
        Returns (urls, errors).
        """
        max_pages = budgets.get("max_pages", _MAX_PAGES)
        visited: set[str] = set()
        queue: list[str] = [start_url]
        collected: list[str] = []
        crawl_errors: list[ReconError] = []

        while queue and len(visited) < max_pages:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)

            html, error = await self._fetch_with_backoff(client, url)
            if error:
                crawl_errors.append(error)
                continue

            soup = BeautifulSoup(html, "html.parser")
            collected.append(url)

            for link_tag in soup.find_all("a", href=True):
                href: str = link_tag["href"]
                abs_url = urljoin(url, href).split("#")[0]
                parsed = urlparse(abs_url)
                if (
                    parsed.netloc == domain
                    and parsed.scheme in ("http", "https")
                    and abs_url not in visited
                    and abs_url not in queue
                ):
                    queue.append(abs_url)

        return collected, crawl_errors

    # --- Fetching with backoff ---

    async def _fetch_with_backoff(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[str, ReconError | None]:
        """
        Fetch a URL with exponential backoff on 429.
        Returns (html_text, error_or_None).
        """
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

    # --- Page parsing & Fact creation ---

    def _page_to_fact(
        self,
        html: str,
        url: str,
        run_id: str,
        scope_features: list[str],
    ) -> Fact | None:
        """
        Parse a help article page and produce a Fact.

        Returns None if the page has insufficient content.
        """
        try:
            soup = BeautifulSoup(html, "html.parser")

            title = soup.title.get_text(strip=True) if soup.title else ""
            # Strip common suffix patterns like " | Trello Support"
            title = re.sub(r"\s*[|\-–]\s*.+$", "", title).strip() or title

            body_text = _extract_article_text(soup)

            if len(body_text) < _MIN_CONTENT_LENGTH:
                return None

            category = _article_category(title, body_text)
            feature = _infer_feature_from_url_and_title(url, title, scope_features)
            related = _extract_related_features(body_text)

            # Build concise claim
            claim_title = title or url
            claim = f'Help documentation describes "{claim_title}"'
            if related:
                claim += f", mentioning features: {', '.join(related[:3])}"
            claim += "."

            excerpt = body_text[:2000]

            evidence = EvidenceRef(
                source_url=url,
                locator="article",
                source_title=title or None,
                raw_excerpt=excerpt,
            )

            structured_data: dict[str, Any] = {
                "title": title,
                "category": str(category),
                "related_features": related,
                "body_excerpt": excerpt[:500],
                "url": url,
            }

            return Fact(
                feature=feature,
                category=category,
                claim=claim,
                evidence=[evidence],
                source_type=self.source_type,
                structured_data=structured_data,
                module_name=self.name,
                authority=self.authority,
                confidence=Confidence.MEDIUM,
                run_id=run_id,
            )

        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to parse page %s: %s", url, exc)
            return None
