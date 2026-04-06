"""
Changelog Scraper — ChangelogModule.

Fetches and parses release notes, changelogs, and "what's new" pages to
extract feature additions, removals, bug fixes, and deprecations.

Produces Facts with category=BUSINESS_RULE or CONFIGURATION.

Supported formats:
  - HTML changelog / release notes pages
  - GitHub Releases pages
  - "What's New" / release blog pages

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
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from bs4 import BeautifulSoup, Tag

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

# Max pages to crawl per run (safety valve)
_MAX_PAGES = 20

# Recent entry threshold — entries published within this window get higher confidence
_RECENT_DAYS = 30

# Well-known changelog URL slugs to probe relative to the base URL
_CHANGELOG_PROBE_PATHS = [
    "/changelog",
    "/release-notes",
    "/releases",
    "/whats-new",
    "/what-s-new",
    "/updates",
    "/blog/release",
    "/blog/changelog",
    "/docs/changelog",
    "/docs/release-notes",
    "/docs/whats-new",
]

# Keywords that classify an entry's change type
_ADDITION_KEYWORDS = re.compile(
    r"\b(add(ed)?|new|introduc(e|ed)|launch(ed)?|announc(e|ed)|now support|feature|releas(e|ed))\b",
    re.IGNORECASE,
)
_REMOVAL_KEYWORDS = re.compile(
    r"\b(remov(e|ed)|deprecat(e|ed)|discontinu(e|ed)|drop(ped)?|sunset(ted)?|end.of.life|retired)\b",
    re.IGNORECASE,
)
_FIX_KEYWORDS = re.compile(
    r"\b(fix(ed)?|bug\s*fix|patch(ed)?|resolv(e|ed)|correct(ed)?|repair(ed)?|address(ed)?)\b",
    re.IGNORECASE,
)
_DEPRECATION_KEYWORDS = re.compile(
    r"\b(deprecat(e|ed|ion)|legacy|will be remov(e|ed)|planned removal)\b",
    re.IGNORECASE,
)

# Date patterns in changelog headers (ISO, US, EU)
_DATE_RE = re.compile(
    r"""
    (?:
        (\d{4})-(\d{1,2})-(\d{1,2})   # ISO: 2024-01-15
        |
        (\w+)\s+(\d{1,2}),?\s+(\d{4})  # US: January 15, 2024
        |
        (\d{1,2})\s+(\w+)\s+(\d{4})    # EU: 15 January 2024
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)

_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


# ---------------------------------------------------------------------------
# Internal data containers
# ---------------------------------------------------------------------------


@dataclass
class _ChangeEntry:
    """A single parsed changelog entry (one release or one bullet point)."""

    title: str  # Version string or date heading
    description: str  # Full text of the entry
    change_type: str  # "addition" | "removal" | "fix" | "deprecation" | "general"
    published_at: str | None  # ISO 8601 date string, if parseable
    source_url: str
    raw_excerpt: str
    feature_hint: str = ""  # Inferred feature from context
    tags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Module
# ---------------------------------------------------------------------------


class ChangelogModule(ReconModule):
    """
    Scrapes changelog, release notes, and "what's new" pages.

    Strategy:
      1. Use module_config["changelog_url"] if provided.
      2. Probe well-known changelog paths relative to the target base URL.
      3. Parse each page for release entries (headings + lists).
      4. Emit one Fact per entry, classified by change type.
    """

    # --- ReconModule interface ---

    @property
    def name(self) -> str:
        return "changelog"

    @property
    def authority(self) -> Authority:
        return Authority.ANECDOTAL

    @property
    def source_type(self) -> SourceType:
        return SourceType.CHANGELOG

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
        Execute changelog recon.

        ENSURES: ReconResult.module == "changelog".
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

        emit("init", f"Starting changelog recon for {request.target}")

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
                changelog_urls = self._discover_urls(base_url, request.module_config)
                emit(
                    "discover",
                    f"Probing {len(changelog_urls)} candidate changelog URL(s)",
                )

                entries: list[_ChangeEntry] = []
                for url in changelog_urls[:_MAX_PAGES]:
                    emit("discover", f"Fetching {url}")
                    html, error = await self._fetch_with_backoff(client, url)
                    if error:
                        errors.append(error)
                        continue

                    urls_visited.append(url)
                    soup = BeautifulSoup(html, "html.parser")
                    page_entries = self._parse_changelog_page(soup, url)
                    entries.extend(page_entries)

                    # If explicit URL was given, don't probe further
                    if request.module_config.get("changelog_url") and page_entries:
                        break
                    elif page_entries:
                        # Found a working probe path — no need to try others
                        break

                emit(
                    "extract",
                    f"Extracted {len(entries)} change entries, building facts",
                    completed=len(entries),
                    total=len(entries),
                )

                scope_features = (
                    request.scope.feature_keys() if request.scope.resolved_features else []
                )

                for entry in entries:
                    fact = self._entry_to_fact(entry, request.run_id, scope_features)
                    facts.append(fact)

            finally:
                if own_client:
                    await client.aclose()

        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error in ChangelogModule.run")
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
                "entries_found": len(facts),
                "errors": len(errors),
                "urls_visited": len(urls_visited),
            },
        )

    # --- URL discovery ---

    def _discover_urls(
        self, base_url: str, module_config: dict[str, Any]
    ) -> list[str]:
        """Return ordered list of URLs to try, explicit config first."""
        explicit: str | None = module_config.get("changelog_url")
        if explicit:
            return [explicit]

        base = base_url.rstrip("/")
        return [f"{base}{path}" for path in _CHANGELOG_PROBE_PATHS]

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

    # --- HTML parsing ---

    def _parse_changelog_page(
        self, soup: BeautifulSoup, page_url: str
    ) -> list[_ChangeEntry]:
        """
        Parse a changelog HTML page into structured entries.

        Heuristics (in order):
          1. Heading-scoped sections: h2/h3 as release title, following
             lists as change bullets.
          2. Article/section elements with release-like headings.
          3. Definition lists (dt/dd pairs).
        """
        entries: list[_ChangeEntry] = []

        # Remove nav, footer, header noise
        for tag in soup.find_all(["nav", "footer", "header", "script", "style"]):
            tag.decompose()

        # --- Heuristic 1: heading-scoped sections ---
        headings = soup.find_all(["h2", "h3"])
        for heading in headings:
            heading_text = heading.get_text(" ", strip=True)
            if not heading_text:
                continue

            # Collect all sibling content until the next same-level heading
            section_parts: list[str] = []
            for sibling in heading.find_next_siblings():
                if not isinstance(sibling, Tag):
                    continue
                if sibling.name in ("h2", "h3"):
                    break
                section_parts.append(sibling.get_text(" ", strip=True))

            section_text = " ".join(section_parts).strip()
            if not section_text:
                continue

            # Parse each bullet within the section as a separate entry,
            # or treat the whole section as one entry if no bullets found
            bullets = heading.find_next_sibling(["ul", "ol"])
            if bullets and isinstance(bullets, Tag):
                items = bullets.find_all("li")
                for item in items:
                    item_text = item.get_text(" ", strip=True)
                    if not item_text:
                        continue
                    entry = self._build_entry(
                        title=heading_text,
                        description=item_text,
                        source_url=page_url,
                        raw_excerpt=f"{heading_text}: {item_text}"[:2000],
                    )
                    entries.append(entry)
            else:
                entry = self._build_entry(
                    title=heading_text,
                    description=section_text,
                    source_url=page_url,
                    raw_excerpt=f"{heading_text}: {section_text}"[:2000],
                )
                entries.append(entry)

        # --- Heuristic 2: if no heading-based entries found, try article elements ---
        if not entries:
            for article in soup.find_all(["article", "section"]):
                if not isinstance(article, Tag):
                    continue
                heading_el = article.find(["h1", "h2", "h3", "h4"])
                title = heading_el.get_text(" ", strip=True) if heading_el else ""
                body_text = article.get_text(" ", strip=True)
                if not body_text or len(body_text) < 20:
                    continue
                entry = self._build_entry(
                    title=title or page_url,
                    description=body_text,
                    source_url=page_url,
                    raw_excerpt=body_text[:2000],
                )
                entries.append(entry)

        return entries

    def _build_entry(
        self,
        title: str,
        description: str,
        source_url: str,
        raw_excerpt: str,
    ) -> _ChangeEntry:
        """Classify an entry and extract date if present."""
        change_type = self._classify_change_type(title + " " + description)
        published_at = self._extract_date(title)

        return _ChangeEntry(
            title=title,
            description=description,
            change_type=change_type,
            published_at=published_at,
            source_url=source_url,
            raw_excerpt=raw_excerpt,
            feature_hint=self._infer_feature_hint(title + " " + description),
        )

    # --- Classification helpers ---

    def _classify_change_type(self, text: str) -> str:
        """Classify a change entry based on keyword patterns."""
        if _DEPRECATION_KEYWORDS.search(text):
            return "deprecation"
        if _REMOVAL_KEYWORDS.search(text):
            return "removal"
        if _FIX_KEYWORDS.search(text):
            return "fix"
        if _ADDITION_KEYWORDS.search(text):
            return "addition"
        return "general"

    def _extract_date(self, text: str) -> str | None:
        """Extract a publication date from a heading string."""
        m = _DATE_RE.search(text)
        if not m:
            return None
        try:
            if m.group(1):  # ISO
                return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
            if m.group(4):  # US: Month Day, Year
                month_num = _MONTH_NAMES.get(m.group(4).lower())
                if month_num:
                    return f"{m.group(6)}-{month_num:02d}-{int(m.group(5)):02d}"
            if m.group(7):  # EU: Day Month Year
                month_num = _MONTH_NAMES.get(m.group(8).lower())
                if month_num:
                    return f"{m.group(9)}-{month_num:02d}-{int(m.group(7)):02d}"
        except (IndexError, ValueError):
            pass
        return None

    def _is_recent(self, published_at: str | None) -> bool:
        """Return True if the entry was published within _RECENT_DAYS."""
        if not published_at:
            return False
        try:
            pub_date = datetime.fromisoformat(published_at).replace(tzinfo=UTC)
            cutoff = datetime.now(UTC) - timedelta(days=_RECENT_DAYS)
            return pub_date >= cutoff
        except ValueError:
            return False

    def _infer_feature_hint(self, text: str) -> str:
        """Extract a short feature slug from the entry text."""
        # Strip common stop words and take the first meaningful noun-like token
        words = re.findall(r"[a-z][a-z0-9-]{2,}", text.lower())
        stop_words = {
            "the", "and", "for", "with", "that", "this", "has", "have",
            "been", "are", "was", "now", "new", "fix", "bug", "add",
            "update", "feature", "release", "version", "change", "note",
        }
        for word in words:
            if word not in stop_words:
                return word
        return "changelog"

    # --- Feature inference ---

    def _infer_feature(self, entry: _ChangeEntry, scope_features: list[str]) -> str:
        """Match entry text against known scope features, else use hint."""
        raw_text = (entry.title + " " + entry.description).lower()
        # Normalise for matching: collapse hyphens/underscores to spaces
        text_normalised = re.sub(r"[-_]", " ", raw_text)

        for feature in scope_features:
            # Match against both hyphenated and space-separated forms
            feature_space = re.sub(r"[-_]", " ", feature.lower())
            feature_hyphen = re.sub(r"[\s_]", "-", feature.lower())
            if feature_space in text_normalised or feature_hyphen in raw_text:
                return feature

        hint = entry.feature_hint
        if hint and hint != "changelog":
            return re.sub(r"[^a-z0-9-]", "-", hint).strip("-") or "changelog"

        return "changelog"

    # --- Fact creation ---

    def _entry_to_fact(
        self,
        entry: _ChangeEntry,
        run_id: str,
        scope_features: list[str],
    ) -> Fact:
        """Convert a _ChangeEntry to a Fact."""
        feature = self._infer_feature(entry, scope_features)

        # Build human-readable claim
        type_phrases = {
            "addition": "introduces or adds",
            "removal": "removes or discontinues",
            "fix": "fixes or resolves",
            "deprecation": "deprecates",
            "general": "describes a change to",
        }
        verb = type_phrases.get(entry.change_type, "describes a change to")
        desc_preview = entry.description[:200].rstrip()
        claim = f"Changelog entry '{entry.title}' {verb}: {desc_preview}"

        # Choose category based on change type
        if entry.change_type in ("addition", "removal", "general"):
            category = FactCategory.BUSINESS_RULE
        else:
            category = FactCategory.CONFIGURATION

        # Recent entries get higher confidence
        confidence = (
            Confidence.MEDIUM if self._is_recent(entry.published_at) else Confidence.LOW
        )

        evidence = EvidenceRef(
            source_url=entry.source_url,
            locator=entry.title[:200] if entry.title else None,
            source_title=entry.title[:200] if entry.title else None,
            published_at=entry.published_at,
            raw_excerpt=entry.raw_excerpt[:2000] if entry.raw_excerpt else None,
        )

        structured_data: dict[str, Any] = {
            "title": entry.title,
            "description": entry.description,
            "change_type": entry.change_type,
            "published_at": entry.published_at,
            "source_url": entry.source_url,
            "tags": entry.tags,
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
            confidence=confidence,
            run_id=run_id,
        )
