"""
Community Scraper — CommunityModule.

Gathers community intelligence from Reddit by searching public subreddits
using Reddit's JSON API (no authentication required).

High-signal filter targets:
  - Complaints and pain points
  - Workarounds and hacks
  - Feature requests
  - Praise and differentiators

Produces Facts with category=BUSINESS_RULE or USER_FLOW and
authority=ANECDOTAL.

INV-020: run() MUST NOT raise.
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

import httpx

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
    "Accept": "application/json",
}

# Subreddits to search (target-specific subreddit added dynamically)
_BASE_SUBREDDITS = ["productivity", "projectmanagement"]

# Reddit JSON API base
_REDDIT_BASE = "https://www.reddit.com"

# Max posts to fetch per subreddit search
_MAX_POSTS_PER_QUERY = 25

# Max total posts to process (safety valve)
_MAX_TOTAL_POSTS = 100

# Back-off schedule for rate limits (seconds)
_BACKOFF_SCHEDULE = [2, 5, 15, 30]

# Signal keywords: post is high-signal if it contains any of these
_SIGNAL_PATTERNS = [
    # Complaints / pain points
    r"\b(broken|bug|issue|problem|frustrat|annoy|hate|terrible|awful|slow|crash|fail)\w*\b",
    # Workarounds
    r"\b(workaround|hack|trick|tip|instead|alternative|replace|switch(?:ed|ing)?)\w*\b",
    # Feature requests
    r"\b(wish|want|would be nice|feature request|please add|missing|need[s]?)\b",
    # Praise / differentiators
    r"\b(love|great|best|awesome|killer feature|better than|switched from)\b",
]
_SIGNAL_RE = re.compile("|".join(_SIGNAL_PATTERNS), re.IGNORECASE)

# Noise filters: skip posts whose titles match these
_NOISE_PATTERNS = [
    r"^\[?(hiring|job|promo|sale|discount|referral|giveaway|ama)\]?",
    r"\b(meme|off.?topic|unrelated)\b",
]
_NOISE_RE = re.compile("|".join(_NOISE_PATTERNS), re.IGNORECASE)

# Category routing: match claim text to a FactCategory
_COMPLAINT_RE = re.compile(
    r"\b(broken|bug|issue|problem|frustrat|annoy|hate|terrible|awful|slow|crash|fail)\w*\b",
    re.IGNORECASE,
)
_FLOW_RE = re.compile(
    r"\b(workaround|workflow|step[s]?|process|how to|instead|alternative)\w*\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Module
# ---------------------------------------------------------------------------


class CommunityModule(ReconModule):
    """
    Scrapes Reddit for community intelligence about the target product.

    Strategy:
      1. Build a list of subreddits: r/{target_name} + base subreddits.
      2. Search each subreddit for the target name via Reddit's JSON API.
      3. Filter posts for high-signal content.
      4. Convert each high-signal post into a Fact.
    """

    # --- ReconModule interface ---

    @property
    def name(self) -> str:
        return "community"

    @property
    def authority(self) -> Authority:
        return Authority.ANECDOTAL

    @property
    def source_type(self) -> SourceType:
        return SourceType.COMMUNITY

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
        Execute Reddit community recon.

        ENSURES: ReconResult.module == "community".
        ENSURES: run() does not raise (INV-020).
        """
        started_at = datetime.now(UTC).isoformat()
        t0 = time.monotonic()

        def emit(
            phase: str, message: str, completed: int | None = None, total: int | None = None
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

        emit("init", f"Starting community recon for {request.target}")

        facts: list[Fact] = []
        errors: list[ReconError] = []
        urls_visited: list[str] = []

        try:
            # Derive a clean target name for subreddit and search queries
            target_name = self._extract_target_name(request.target)
            subreddits = self._build_subreddit_list(target_name, request.module_config)
            search_query = request.module_config.get("search_query", target_name)

            if services.http_client is not None:
                client = services.http_client
                own_client = False
            else:
                client = httpx.AsyncClient(
                    headers=_DEFAULT_HEADERS,
                    follow_redirects=True,
                    timeout=20.0,
                )
                own_client = True

            try:
                total_subreddits = len(subreddits)
                all_posts: list[dict[str, Any]] = []

                for idx, subreddit in enumerate(subreddits):
                    emit(
                        "discover",
                        f"Searching r/{subreddit} for '{search_query}'",
                        completed=idx,
                        total=total_subreddits,
                    )

                    posts, url, error = await self._search_subreddit(
                        client, subreddit, search_query
                    )
                    if url:
                        urls_visited.append(url)
                    if error:
                        errors.append(error)
                    else:
                        all_posts.extend(posts)

                    if len(all_posts) >= _MAX_TOTAL_POSTS:
                        break

                emit(
                    "extract",
                    f"Fetched {len(all_posts)} posts, filtering for signal",
                )

                high_signal = self._filter_high_signal(all_posts)

                emit(
                    "extract",
                    f"{len(high_signal)} high-signal posts found, building facts",
                    completed=len(high_signal),
                    total=len(high_signal),
                )

                for post in high_signal:
                    fact = self._post_to_fact(post, request.run_id, target_name)
                    facts.append(fact)

            finally:
                if own_client:
                    await client.aclose()

        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error in CommunityModule.run")
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

    # --- Subreddit search ---

    async def _search_subreddit(
        self,
        client: httpx.AsyncClient,
        subreddit: str,
        query: str,
    ) -> tuple[list[dict[str, Any]], str, ReconError | None]:
        """
        Search a single subreddit using Reddit's JSON API.

        Returns (posts, url_fetched, error_or_None).
        Posts are raw Reddit post data dicts (the "data" child objects).
        """
        url = (
            f"{_REDDIT_BASE}/r/{subreddit}/search.json"
            f"?q={query}&restrict_sr=1&sort=top&limit={_MAX_POSTS_PER_QUERY}&t=all"
        )

        for attempt, backoff in enumerate([0] + _BACKOFF_SCHEDULE):
            if backoff:
                await asyncio.sleep(backoff)

            try:
                resp = await client.get(url)
            except httpx.TimeoutException:
                return [], url, ReconError(
                    source_url=url,
                    error_type="timeout",
                    message=f"Request timed out: {url}",
                    recoverable=True,
                )
            except httpx.RequestError as exc:
                return [], url, ReconError(
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
                return [], url, ReconError(
                    source_url=url,
                    error_type="rate_limited",
                    message=f"Rate limited after {attempt + 1} attempts: {url}",
                    recoverable=True,
                )

            if resp.status_code == 404:
                # Subreddit doesn't exist — not an error, just skip
                logger.debug("Subreddit r/%s not found (404), skipping", subreddit)
                return [], url, None

            if resp.status_code >= 400:
                return [], url, ReconError(
                    source_url=url,
                    error_type="parse_error",
                    message=f"HTTP {resp.status_code} fetching {url}",
                    recoverable=resp.status_code >= 500,
                )

            # Parse JSON
            try:
                data = resp.json()
            except Exception as exc:  # noqa: BLE001
                return [], url, ReconError(
                    source_url=url,
                    error_type="parse_error",
                    message=f"JSON parse error from {url}: {exc}",
                    recoverable=False,
                )

            posts = self._extract_posts_from_listing(data)
            return posts, url, None

        return [], url, ReconError(
            source_url=url,
            error_type="rate_limited",
            message=f"Exhausted retries for {url}",
            recoverable=True,
        )

    def _extract_posts_from_listing(self, data: Any) -> list[dict[str, Any]]:
        """Extract post data dicts from a Reddit listing response."""
        posts: list[dict[str, Any]] = []
        try:
            children = data["data"]["children"]
            for child in children:
                if child.get("kind") == "t3":  # t3 = link/post
                    post_data = child.get("data", {})
                    posts.append(post_data)
        except (KeyError, TypeError, AttributeError):
            logger.debug("Unexpected Reddit listing structure")
        return posts

    # --- Signal filtering ---

    def _filter_high_signal(self, posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Return only posts that are high-signal and not noise.

        High-signal: title or selftext matches _SIGNAL_RE.
        Noise: title matches _NOISE_RE or score < 2 (too low engagement).
        """
        results: list[dict[str, Any]] = []
        for post in posts:
            title: str = post.get("title", "")
            selftext: str = post.get("selftext", "")
            score: int = post.get("score", 0)

            # Skip noise
            if _NOISE_RE.search(title):
                continue

            # Skip very low-engagement posts
            if score < 2:
                continue

            # Require at least one signal keyword in title or body
            combined = f"{title} {selftext}"
            if _SIGNAL_RE.search(combined):
                results.append(post)

        return results

    # --- Fact creation ---

    def _post_to_fact(
        self,
        post: dict[str, Any],
        run_id: str,
        target_name: str,
    ) -> Fact:
        """Convert a Reddit post dict to a Fact."""
        title: str = post.get("title", "")
        selftext: str = post.get("selftext", "")
        permalink: str = post.get("permalink", "")
        subreddit: str = post.get("subreddit", "")
        score: int = post.get("score", 0)
        num_comments: int = post.get("num_comments", 0)
        url: str = post.get("url", "")
        created_utc: float = post.get("created_utc", 0.0)

        source_url = f"{_REDDIT_BASE}{permalink}" if permalink else url or _REDDIT_BASE

        # Build a concise claim from the post title and first 300 chars of body
        body_snippet = selftext[:300].strip() if selftext else ""
        claim = f"Reddit community post in r/{subreddit}: {title}"
        if body_snippet:
            claim += f" — {body_snippet}"

        # Determine category: complaints/bugs → BUSINESS_RULE, workarounds/flows → USER_FLOW
        claim_text = f"{title} {selftext}"
        if _FLOW_RE.search(claim_text):
            category = FactCategory.USER_FLOW
        else:
            category = FactCategory.BUSINESS_RULE

        # Confidence based on engagement
        if score >= 50 or num_comments >= 20:
            confidence = Confidence.MEDIUM
        else:
            confidence = Confidence.LOW

        # Published timestamp
        published_at: str | None = None
        if created_utc:
            published_at = datetime.fromtimestamp(created_utc, tz=UTC).isoformat()

        evidence = EvidenceRef(
            source_url=source_url,
            locator=f"r/{subreddit}",
            source_title=title[:200],
            raw_excerpt=(f"{title}\n\n{selftext}"[:2000]).strip() or None,
            published_at=published_at,
        )

        structured_data: dict[str, Any] = {
            "subreddit": subreddit,
            "title": title,
            "score": score,
            "num_comments": num_comments,
            "permalink": source_url,
            "target": target_name,
        }

        return Fact(
            feature=self._infer_feature(title, selftext, target_name),
            category=category,
            claim=claim[:2000],
            evidence=[evidence],
            source_type=self.source_type,
            structured_data=structured_data,
            module_name=self.name,
            authority=self.authority,
            confidence=confidence,
            run_id=run_id,
        )

    # --- Helpers ---

    def _extract_target_name(self, target: str) -> str:
        """
        Derive a clean product name from the target.

        E.g. "trello.com" → "trello", "asana.com" → "asana".
        """
        # Strip scheme if present
        name = re.sub(r"^https?://", "", target)
        # Take the first part before the first dot
        name = name.split(".")[0]
        # Clean to alphanumeric + hyphens
        name = re.sub(r"[^a-z0-9-]", "", name.lower())
        return name or target

    def _build_subreddit_list(
        self, target_name: str, module_config: dict[str, Any]
    ) -> list[str]:
        """
        Build the list of subreddits to search.

        Priority: module_config override > target subreddit + base subreddits.
        """
        if "subreddits" in module_config:
            return list(module_config["subreddits"])
        subreddits = [target_name] + _BASE_SUBREDDITS
        return subreddits

    def _infer_feature(self, title: str, body: str, target_name: str) -> str:
        """
        Infer a feature key from post content.

        Looks for product nouns in the title that suggest a specific feature area.
        Falls back to "community-feedback".
        """
        # Common feature-area keywords → feature key mapping
        _FEATURE_MAP = [
            (r"\b(board[s]?|kanban|card[s]?|list[s]?)\b", "boards"),
            (r"\b(notif(?:ication)?[s]?|alert[s]?|email[s]?)\b", "notifications"),
            (r"\b(integrat(?:ion)?[s]?|connect|sync|webhook)\b", "integrations"),
            (r"\b(permiss(?:ion)?[s]?|role[s]?|access|admin)\b", "permissions"),
            (r"\b(search|filter|sort)\b", "search"),
            (r"\b(mobile|ios|android|app)\b", "mobile"),
            (r"\b(import|export|backup|migrate)\b", "data-portability"),
            (r"\b(pric(?:e|ing)|cost|plan|tier|subscription)\b", "pricing"),
            (r"\b(login|auth(?:entication)?|sso|oauth|password)\b", "auth"),
            (r"\b(api|webhook|developer|sdk)\b", "api"),
        ]

        combined = f"{title} {body}"
        for pattern, feature_key in _FEATURE_MAP:
            if re.search(pattern, combined, re.IGNORECASE):
                return feature_key

        return "community-feedback"
