"""
Help Center Scraper — HelpCenterModule.

Fetches and parses user-facing help documentation (help center, knowledge base,
FAQs) to extract feature descriptions, user flows, and UI component behavior.

Produces Facts with category=USER_FLOW or UI_COMPONENT.

Supported sources:
  - sitemap.xml (preferred — discovers all article URLs)
  - Index/help-center landing page (fallback — crawls same-domain links)

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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Back-off schedule for 429 responses (seconds)
_BACKOFF_SCHEDULE = [2, 5, 15, 30]

# Max articles to crawl per run (safety valve)
_MAX_PAGES = 100

# Well-known help center URL slugs to probe (relative to base URL)
_HELP_CENTER_PROBE_PATHS = [
    "/sitemap.xml",
    "/help",
    "/help-center",
    "/support",
    "/docs",
    "/knowledge-base",
    "/faq",
    "/en/support",
    "/hc/en-us",
    "/hc",
]

# Minimum article body length to be considered a real article (noise filter)
_MIN_ARTICLE_LENGTH = 100

# UI / flow keywords for category classification
_USER_FLOW_KEYWORDS = re.compile(
    r"\b(how to|steps?|workflow|process|click|navigate|go to|open|select|"
    r"drag|drop|create|add|invite|share|export|import|move|archive|delete|"
    r"sign (in|up|out)|log (in|out)|getting started|tutorial)\b",
    re.IGNORECASE,
)

_UI_COMPONENT_KEYWORDS = re.compile(
    r"\b(button|menu|sidebar|modal|dialog|panel|card|board|list|column|"
    r"toolbar|dropdown|toggle|checkbox|icon|badge|label|tag|filter|"
    r"search bar|notification|tooltip|banner|header|footer|tab)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Internal data containers
# ---------------------------------------------------------------------------


@dataclass
class _HelpArticle:
    """A single parsed help center article."""

    title: str
    body: str  # Plain-text article body
    category: str  # Article category/section heading (if available)
    source_url: str
    raw_excerpt: str
    tags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Module
# ---------------------------------------------------------------------------


class HelpCenterModule(ReconModule):
    """
    Scrapes user-facing help documentation.

    Strategy:
      1. Try to fetch sitemap.xml and extract article URLs.
      2. Fall back to crawling help-center index pages (same-domain links).
      3. Parse each article for title, body, and category context.
      4. Emit one Fact per article, categorised as USER_FLOW or UI_COMPONENT.
    """

    # --- ReconModule interface ---

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
        return []  # Public help center — no credentials needed

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
                max_pages = request.budgets.get("max_pages", _MAX_PAGES)
                explicit_url: str | None = request.module_config.get("help_center_url")

                # Step 1: discover article URLs
                article_urls: list[str] = []
                if explicit_url:
                    emit("discover", f"Using explicit help center URL: {explicit_url}")
                    sitemap_url = explicit_url.rstrip("/") + "/sitemap.xml"
                    sitemap_urls, sitemap_error = await self._try_sitemap(client, sitemap_url)
                    if sitemap_urls:
                        article_urls = sitemap_urls
                    else:
                        # Crawl from the explicit URL
                        crawled, crawl_errors = await self._crawl_index(
                            client, explicit_url, max_pages, emit
                        )
                        article_urls = crawled
                        errors.extend(crawl_errors)
                else:
                    # Probe well-known paths
                    sitemap_url = base_url.rstrip("/") + "/sitemap.xml"
                    emit("discover", f"Probing sitemap at {sitemap_url}")
                    sitemap_urls, sitemap_error = await self._try_sitemap(client, sitemap_url)
                    if sitemap_urls:
                        article_urls = sitemap_urls[:max_pages]
                        emit(
                            "discover",
                            f"Found {len(sitemap_urls)} URLs in sitemap, "
                            f"capped at {len(article_urls)}",
                        )
                    else:
                        # Fall back to crawling index pages
                        emit("discover", "No sitemap found, crawling help center index pages")
                        index_url = self._find_index_url(base_url, request.module_config)
                        crawled, crawl_errors = await self._crawl_index(
                            client, index_url, max_pages, emit
                        )
                        article_urls = crawled
                        errors.extend(crawl_errors)

                emit(
                    "extract",
                    f"Fetching and parsing {len(article_urls)} article(s)",
                    completed=0,
                    total=len(article_urls),
                )

                scope_features = (
                    request.scope.feature_keys() if request.scope.resolved_features else []
                )

                for idx, url in enumerate(article_urls[:max_pages]):
                    emit("extract", f"Parsing {url}", completed=idx + 1, total=len(article_urls))
                    html, error = await self._fetch_with_backoff(client, url)
                    if error:
                        errors.append(error)
                        continue

                    urls_visited.append(url)
                    soup = BeautifulSoup(html, "html.parser")
                    article = self._parse_article(soup, url)
                    if article is None:
                        continue

                    fact = self._article_to_fact(article, request.run_id, scope_features)
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

    # --- Sitemap discovery ---

    async def _try_sitemap(
        self, client: httpx.AsyncClient, sitemap_url: str
    ) -> tuple[list[str], ReconError | None]:
        """
        Fetch and parse sitemap.xml.

        Returns (article_urls, error_or_None). Article URLs are filtered to
        same-domain only and sorted by apparent relevance (help/support paths first).
        """
        try:
            resp = await client.get(sitemap_url)
        except (httpx.RequestError, httpx.TimeoutException) as exc:
            return [], ReconError(
                source_url=sitemap_url,
                error_type="parse_error",
                message=f"Could not fetch sitemap: {exc}",
                recoverable=True,
            )

        if resp.status_code != 200:
            return [], None  # No sitemap — not an error, just not present

        try:
            soup = BeautifulSoup(resp.text, "xml")
            locs = [tag.get_text(strip=True) for tag in soup.find_all("loc")]
            if not locs:
                # Try HTML parser as fallback for malformed XML
                soup = BeautifulSoup(resp.text, "html.parser")
                locs = [tag.get_text(strip=True) for tag in soup.find_all("loc")]

            domain = urlparse(sitemap_url).netloc
            same_domain = [u for u in locs if urlparse(u).netloc == domain]

            # Prioritise URLs that look like help articles
            def _score(url: str) -> int:
                lower = url.lower()
                for kw in ("/help", "/support", "/hc/", "/knowledge", "/faq", "/docs", "/article"):
                    if kw in lower:
                        return 0
                return 1

            return sorted(same_domain, key=_score), None

        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to parse sitemap at %s: %s", sitemap_url, exc)
            return [], None

    # --- Index crawl fallback ---

    def _find_index_url(self, base_url: str, module_config: dict[str, Any]) -> str:
        """Return the best index URL to start crawling from."""
        explicit: str | None = module_config.get("help_center_url")
        if explicit:
            return explicit
        base = base_url.rstrip("/")
        # Return the first probe path — caller will iterate if needed
        return f"{base}{_HELP_CENTER_PROBE_PATHS[1]}"  # /help

    async def _crawl_index(
        self,
        client: httpx.AsyncClient,
        start_url: str,
        max_pages: int,
        emit: Callable[[str, str, int | None, int | None], None],
    ) -> tuple[list[str], list[ReconError]]:
        """
        Crawl help center pages starting from start_url.

        Follows same-domain links, collects article-looking URLs.
        Returns (article_urls, errors).
        """
        domain = urlparse(start_url).netloc
        visited: set[str] = set()
        queue: list[str] = [start_url]
        article_urls: list[str] = []
        crawl_errors: list[ReconError] = []

        while queue and len(visited) < max_pages:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)

            emit("discover", f"Crawling {url}", len(visited), max_pages)
            html, error = await self._fetch_with_backoff(client, url)
            if error:
                crawl_errors.append(error)
                continue

            soup = BeautifulSoup(html, "html.parser")
            article_urls.append(url)

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

        return article_urls, crawl_errors

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

    # --- HTML article parsing ---

    def _parse_article(self, soup: BeautifulSoup, page_url: str) -> _HelpArticle | None:
        """
        Parse a help center page into a structured article.

        Returns None if the page has insufficient content to be a real article.
        """
        # Remove noise elements
        for tag in soup.find_all(["nav", "footer", "header", "script", "style", "aside"]):
            tag.decompose()

        # Extract title
        title = ""
        if soup.title:
            title = soup.title.get_text(strip=True)
        for heading_tag in ("h1", "h2"):
            h = soup.find(heading_tag)
            if h:
                title = h.get_text(strip=True)
                break

        # Extract article body — prefer <article> or <main>, else <body>
        body_el = soup.find("article") or soup.find("main") or soup.body
        body = body_el.get_text(" ", strip=True) if body_el else soup.get_text(" ", strip=True)

        # Skip pages that are too thin to be real articles
        if len(body) < _MIN_ARTICLE_LENGTH:
            return None

        # Extract section/category from breadcrumb or nav-like structure
        category = self._extract_category(soup)

        # Extract any explicit tags from meta keywords or category pills
        tags = self._extract_tags(soup)

        return _HelpArticle(
            title=title or page_url,
            body=body,
            category=category,
            source_url=page_url,
            raw_excerpt=body[:2000],
            tags=tags,
        )

    def _extract_category(self, soup: BeautifulSoup) -> str:
        """Extract a category label from breadcrumbs or section headings."""
        # Try structured breadcrumb
        for breadcrumb_cls in ("breadcrumb", "breadcrumbs", "nav-breadcrumb"):
            bc = soup.find(class_=re.compile(breadcrumb_cls, re.I))
            if bc:
                items = bc.find_all(["a", "li", "span"])
                # Take the second-to-last item as the category
                texts = [i.get_text(strip=True) for i in items if i.get_text(strip=True)]
                if len(texts) >= 2:
                    return texts[-2]

        # Try schema.org BreadcrumbList
        bc_list = soup.find(attrs={"itemtype": re.compile(r"BreadcrumbList", re.I)})
        if bc_list:
            items = bc_list.find_all(attrs={"itemprop": "name"})
            texts = [i.get_text(strip=True) for i in items]
            if len(texts) >= 2:
                return texts[-2]

        return ""

    def _extract_tags(self, soup: BeautifulSoup) -> list[str]:
        """Extract tags from meta keywords or visible tag/label elements."""
        tags: list[str] = []

        # Meta keywords
        meta_kw = soup.find("meta", attrs={"name": re.compile(r"keyword", re.I)})
        if meta_kw and meta_kw.get("content"):
            tags.extend(
                [t.strip() for t in str(meta_kw["content"]).split(",") if t.strip()][:10]
            )

        # Visible tag pills
        for el in soup.find_all(class_=re.compile(r"\btag\b|\blabel\b|\bcategory\b", re.I)):
            text = el.get_text(strip=True)
            if text and len(text) < 50:
                tags.append(text)

        return list(dict.fromkeys(tags))[:10]  # deduplicate, cap at 10

    # --- Category classification ---

    def _classify_fact_category(self, article: _HelpArticle) -> FactCategory:
        """Classify article as USER_FLOW or UI_COMPONENT based on content."""
        text = article.title + " " + article.body[:500]
        if _USER_FLOW_KEYWORDS.search(text):
            return FactCategory.USER_FLOW
        if _UI_COMPONENT_KEYWORDS.search(text):
            return FactCategory.UI_COMPONENT
        # Default to USER_FLOW for help content
        return FactCategory.USER_FLOW

    # --- Feature inference ---

    def _infer_feature(self, article: _HelpArticle, scope_features: list[str]) -> str:
        """Match article text against scope features, else derive from title/category."""
        text = (article.title + " " + article.category + " " + article.body[:300]).lower()
        text_normalised = re.sub(r"[-_]", " ", text)

        for feature in scope_features:
            feature_space = re.sub(r"[-_]", " ", feature.lower())
            feature_hyphen = re.sub(r"[\s_]", "-", feature.lower())
            if feature_space in text_normalised or feature_hyphen in text:
                return feature

        # Derive from title
        words = re.findall(r"[a-z][a-z0-9]{2,}", article.title.lower())
        stop_words = {
            "how", "the", "and", "for", "with", "that", "this", "use", "using",
            "get", "set", "new", "your", "you", "can", "what", "why", "when",
        }
        for word in words:
            if word not in stop_words:
                return re.sub(r"[^a-z0-9-]", "-", word).strip("-") or "help-center"

        return "help-center"

    # --- Fact creation ---

    def _article_to_fact(
        self,
        article: _HelpArticle,
        run_id: str,
        scope_features: list[str],
    ) -> Fact:
        """Convert a _HelpArticle to a Fact."""
        feature = self._infer_feature(article, scope_features)
        fact_category = self._classify_fact_category(article)

        # Build human-readable claim
        body_preview = article.body[:200].rstrip()
        claim = f"Help article '{article.title}' describes: {body_preview}"

        evidence = EvidenceRef(
            source_url=article.source_url,
            locator=article.title[:200] if article.title else None,
            source_title=article.title[:200] if article.title else None,
            raw_excerpt=article.raw_excerpt[:2000] if article.raw_excerpt else None,
        )

        structured_data: dict[str, Any] = {
            "title": article.title,
            "category": article.category,
            "body_preview": article.body[:500],
            "tags": article.tags,
            "source_url": article.source_url,
        }

        return Fact(
            feature=feature,
            category=fact_category,
            claim=claim,
            evidence=[evidence],
            source_type=self.source_type,
            structured_data=structured_data,
            module_name=self.name,
            authority=self.authority,
            confidence=Confidence.MEDIUM,
            run_id=run_id,
        )
