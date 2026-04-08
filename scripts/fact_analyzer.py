"""
FactAnalyzer — filters, deduplicates, reclassifies, and clusters raw facts.

Between recon and synthesis, this pipeline:
  1. NoiseFilter: rule-based removal of static assets, analytics, tracking
  2. Deduplicator: merges duplicate API endpoints and help-center articles
  3. FeatureReclassifier: fixes garbage feature names using API path and context
  4. SubFeatureClusterer: tags facts with granular sub-feature labels

Public interface:
    analyzer = FactAnalyzer(spec_store, keychain)
    analysis = analyzer.analyze_report(facts)   # returns AnalysisReport
    kept_facts = await analyzer.analyze(facts)  # returns list[Fact] for pipeline
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from scripts.models import (
    Fact,
    FactCategory,
    SourceType,
)
from scripts.spec_store import SpecStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# AnalysisReport
# ---------------------------------------------------------------------------


@dataclass
class AnalysisReport:
    """Summary produced by FactAnalyzer.analyze_report()."""

    total_facts: int
    noise_filtered: int
    deduplicated: int
    kept: int
    clusters: dict[str, list[str]]  # sub_feature -> [fact_ids]
    facts_by_feature: dict[str, int]  # feature -> count
    noise_patterns: dict[str, int]  # pattern_name -> count removed


# ---------------------------------------------------------------------------
# NoiseFilter
# ---------------------------------------------------------------------------

# Static asset extensions — these paths carry no product knowledge
_STATIC_EXTS = frozenset([
    ".js", ".css", ".woff", ".woff2", ".ttf", ".eot", ".svg", ".png",
    ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".map", ".json.gz",
])

# Analytics / tracking / ad-tech path substrings
_ANALYTICS_SUBSTRINGS = [
    "gasv3", "/analytics", "recaptcha", "sentry", "onetrust",
    "scripttemplates", "cookieconsentpub", "pagead", "attribution_trigger",
    "adsct", "activityi", "/gtag", "/collect", "ddm/fls", "/pixels/",
    "rmkt/collect", "gmp/conversion", "webevents", "viewthroughconversion",
    "1p-user-list", "1p-conversion", "rgstr",
]

# CDN / CMS substrings (Contentful, image CDNs)
_CDN_SUBSTRINGS = ["contentful", "rz1oowkt5gyp"]

# Third-party widget substrings
_WIDGET_SUBSTRINGS = ["powerup-loader.html", "flags/api", "flagcdn"]

# Chunk/hash asset — paths like /abc1f2e3.chunk.js or /a1b2c3d4e5f6.js
_CHUNK_HASH_RE = re.compile(r"[a-f0-9]{8,}\.")

# Help-center JS-required error pages
_JS_REQUIRED_RE = re.compile(r"please enable javascript|unable to load", re.IGNORECASE)


class NoiseFilter:
    """
    Rule-based identification of noise facts.

    Returns a set of fact IDs that should be soft-deleted, and a breakdown
    dict mapping rule_name -> count for the AnalysisReport.
    """

    def identify_noise(
        self, facts: list[Fact]
    ) -> tuple[set[str], dict[str, int]]:
        """
        Return (noise_ids, noise_patterns).

        noise_ids: set of fact.id values for noise facts
        noise_patterns: dict[rule_name, count]
        """
        noise_ids: set[str] = set()
        patterns: dict[str, int] = {}

        for fact in facts:
            rule = self._classify(fact)
            if rule:
                noise_ids.add(fact.id)
                patterns[rule] = patterns.get(rule, 0) + 1

        return noise_ids, patterns

    def is_noise(self, fact: Fact) -> bool:
        """Convenience method: return True if fact is noise."""
        return self._classify(fact) is not None

    def _classify(self, fact: Fact) -> str | None:
        """Return the rule name that matched, or None if not noise."""
        # Pull the primary URL from evidence
        url = ""
        if fact.evidence:
            url = fact.evidence[0].source_url or ""

        parsed = urlparse(url)
        path = parsed.path.lower()
        # Full URL lowercased for domain-level checks
        url_lower = url.lower()

        # --- Rule 1: Static asset extensions ---
        for ext in _STATIC_EXTS:
            if path.endswith(ext):
                return "static_extension"

        # --- Rule 2: Chunk / hash assets (webpack bundles) ---
        # Only apply to paths that look like asset paths (not API paths like /1/boards/...)
        if _CHUNK_HASH_RE.search(path) and not path.startswith("/1/"):
            return "chunk_hash_asset"

        # --- Rule 3: Analytics / tracking — check path, domain, and full URL ---
        netloc = parsed.netloc.lower()
        for substr in _ANALYTICS_SUBSTRINGS:
            # Strip leading slash for domain-level matching
            bare = substr.lstrip("/")
            if substr in path or bare in netloc or substr in url_lower:
                return "analytics_tracking"

        # --- Rule 4: CDN / CMS content ---
        for substr in _CDN_SUBSTRINGS:
            if substr in url_lower:
                return "cdn_cms"

        # --- Rule 5: Third-party widget substrings ---
        for substr in _WIDGET_SUBSTRINGS:
            if substr in path or substr in url_lower:
                return "third_party_widget"

        # --- Rule 6: Help-center JS-required error pages ---
        if fact.source_type == SourceType.HELP_CENTER and _JS_REQUIRED_RE.search(fact.claim):
            return "help_center_js_error"

        # --- Rule 7: UI component with trivially short/generic claim ---
        if fact.category == FactCategory.UI_COMPONENT and len(fact.claim.strip()) < 10:
            return "trivial_ui_claim"

        # --- Rule 8: 404 / error page UI facts ---
        if fact.category == FactCategory.UI_COMPONENT:
            lower = fact.claim.lower()
            if "404 not found" in lower or "page not found" in lower:
                return "error_page"

        return None


# ---------------------------------------------------------------------------
# Deduplicator
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_SHORT_ID_RE = re.compile(r"/[a-zA-Z0-9]{8,}(?:/|$)")
_QUERY_RE = re.compile(r"\?.*$")


def _normalize_api_path(path: str) -> str:
    """Strip UUIDs, short IDs, and query strings from an API path."""
    path = _QUERY_RE.sub("", path)
    path = _UUID_RE.sub("{id}", path)
    path = _SHORT_ID_RE.sub("/{id}/", path)
    return path.rstrip("/")


def _api_key(fact: Fact) -> str | None:
    """Return 'METHOD normalized_path' for API endpoint facts, else None."""
    if fact.category != FactCategory.API_ENDPOINT:
        return None
    sd = fact.structured_data
    method = sd.get("method", "GET").upper()
    path = sd.get("path", "")
    if not path and fact.evidence:
        path = urlparse(fact.evidence[0].source_url).path
    return f"{method} {_normalize_api_path(path)}"


def _help_center_key(fact: Fact) -> str | None:
    """Return article title for help-center facts, else None."""
    if fact.source_type != SourceType.HELP_CENTER:
        return None
    title = fact.structured_data.get("title") or fact.claim[:80]
    return title.strip().lower()


def _ws_event_key(fact: Fact) -> str | None:
    """Return (event_name, direction) for WS_EVENT facts, else None."""
    if fact.category != FactCategory.WS_EVENT:
        return None
    sd = fact.structured_data
    name = sd.get("event_name", "")
    direction = sd.get("direction", "")
    return f"ws::{name}::{direction}"


def _richness(fact: Fact) -> tuple[bool, int, int]:
    """Score a fact for 'richness' — used to pick the best representative."""
    has_body = bool(fact.structured_data.get("response_body_sample"))
    body_len = len(str(fact.structured_data.get("response_body_sample", "")))
    auth_rank = fact.authority.rank()
    return (has_body, body_len, auth_rank)


class Deduplicator:
    """
    Identifies duplicate facts within three domains:
    - API endpoints (same METHOD + normalized path)
    - Help-center articles (same title)
    - WebSocket events (same event_name + direction)

    Returns a set of fact IDs to soft-delete (all but the best representative).
    """

    def identify_duplicates(self, facts: list[Fact]) -> set[str]:
        """Return fact IDs that are duplicates (lower-quality copies)."""
        dedup_ids: set[str] = set()

        api_groups: dict[str, list[Fact]] = {}
        hc_groups: dict[str, list[Fact]] = {}
        ws_groups: dict[str, list[Fact]] = {}

        for fact in facts:
            k = _api_key(fact)
            if k:
                api_groups.setdefault(k, []).append(fact)
                continue
            k = _help_center_key(fact)
            if k:
                hc_groups.setdefault(k, []).append(fact)
                continue
            k = _ws_event_key(fact)
            if k:
                ws_groups.setdefault(k, []).append(fact)

        for group in (*api_groups.values(), *hc_groups.values(), *ws_groups.values()):
            if len(group) < 2:
                continue
            best = max(group, key=_richness)
            for fact in group:
                if fact.id != best.id:
                    dedup_ids.add(fact.id)

        return dedup_ids


# ---------------------------------------------------------------------------
# FeatureReclassifier
# ---------------------------------------------------------------------------

# Map API path prefixes to canonical feature names
_API_PATH_TO_FEATURE: list[tuple[str, str]] = [
    ("/1/boards", "boards"),
    ("/1/cards", "cards"),
    ("/1/lists", "lists"),
    ("/1/members", "members"),
    ("/1/organizations", "organizations"),
    ("/1/org", "organizations"),
    ("/1/webhooks", "webhooks"),
    ("/1/tokens", "auth"),
    ("/1/search", "search"),
    ("/1/labels", "labels"),
    ("/1/checklist", "checklists"),
    ("/1/notifications", "notifications"),
    ("/1/type", "boards"),
    ("/gateway/api/graphql", "graphql-subscriptions"),
]

# Map URL page paths to features
_URL_PATH_TO_FEATURE: list[tuple[str, str]] = [
    ("/pricing", "pricing"),
    ("/power-ups", "power-ups"),
    ("/enterprise", "enterprise"),
    ("/templates", "templates"),
    ("/guide", "onboarding"),
    ("/signup", "auth"),
    ("/login", "auth"),
    ("/logout", "auth"),
    ("/w/", "workspaces"),
]

# Garbage feature names that need remapping
_GARBAGE_FEATURES: dict[str, str] = {
    "see": "trello",
    "palace": "trello",
    "get": "trello",
    "quickly": "trello",
    "free": "pricing",
    "capture": "trello",
    "unlimited": "pricing",
    "custom": "configuration",
    "general": "trello",
    "home": "trello",
}


class FeatureReclassifier:
    """
    Produces a mapping of fact_id -> new_feature_name for facts
    that have poor or garbage feature assignments.

    Facts are not mutated — callers receive a dict and can filter
    by it to present corrected data without touching frozen dataclasses.
    """

    def reclassify(self, facts: list[Fact]) -> dict[str, str]:
        """
        Return {fact_id: new_feature} for facts whose feature should change.
        Facts not in the returned dict keep their original feature.
        """
        remaps: dict[str, str] = {}
        for fact in facts:
            new_feature = self._derive_feature(fact)
            if new_feature and new_feature != fact.feature:
                remaps[fact.id] = new_feature
        return remaps

    def _derive_feature(self, fact: Fact) -> str | None:
        """Return the corrected feature, or None to keep existing."""
        current = fact.feature.strip().lower()

        # 1. Garbage feature names — always remap regardless of fact type
        if current in _GARBAGE_FEATURES:
            # Try to derive something better from content first
            derived = self._from_api_path(fact) or self._from_url_path(fact)
            return derived or _GARBAGE_FEATURES[current]

        # 2. API endpoint facts: derive from structured_data.path
        if fact.category == FactCategory.API_ENDPOINT:
            derived = self._from_api_path(fact)
            if derived:
                return derived

        # 3. Any fact: try URL-based mapping
        derived = self._from_url_path(fact)
        if derived:
            return derived

        return None

    def _from_api_path(self, fact: Fact) -> str | None:
        path = fact.structured_data.get("path", "")
        if not path and fact.evidence:
            path = urlparse(fact.evidence[0].source_url).path
        path = path.lower()
        for prefix, feature in _API_PATH_TO_FEATURE:
            if path.startswith(prefix):
                return feature
        return None

    def _from_url_path(self, fact: Fact) -> str | None:
        url = ""
        if fact.evidence:
            url = fact.evidence[0].source_url or ""
        path = urlparse(url).path.lower()
        for prefix, feature in _URL_PATH_TO_FEATURE:
            if prefix in path:
                return feature
        return None


# ---------------------------------------------------------------------------
# SubFeatureClusterer
# ---------------------------------------------------------------------------

# Keyword patterns for clustering within features
_CLUSTER_PATTERNS: dict[str, list[tuple[str, list[str]]]] = {
    "boards": [
        ("board-creation", ["creat", "new board", "add board", "blank board"]),
        ("board-settings", [
            "setting", "background", "color", "visibility", "privacy", "close", "delet",
        ]),
        ("board-sharing", ["shar", "invit", "member", "permission", "role", "access"]),
        ("board-templates", ["template"]),
        ("board-archive", ["archiv", "clos"]),
        ("board-starred", ["star", "favourit", "favorite"]),
    ],
    "cards": [
        ("card-creation", ["creat", "new card", "add card"]),
        ("card-editing", ["edit", "updat", "rename", "title", "description"]),
        ("card-attachments", ["attach", "file", "upload", "link"]),
        ("card-checklists", ["checklist", "todo", "item"]),
        ("card-due-dates", ["due", "date", "deadline", "reminder"]),
        ("card-labels", ["label", "tag", "color"]),
        ("card-members", ["member", "assign"]),
        ("card-moving", ["mov", "drag", "reorder", "sort"]),
    ],
    "auth": [
        ("auth-login", ["login", "sign in", "signin"]),
        ("auth-signup", ["signup", "sign up", "register", "creat.*account"]),
        ("auth-sso", ["sso", "saml", "google", "atlassian", "oauth"]),
        ("auth-permissions", ["permission", "role", "access control"]),
    ],
    "lists": [
        ("list-creation", ["creat", "new list", "add list"]),
        ("list-editing", ["edit", "rename", "archiv"]),
        ("list-moving", ["mov", "reorder"]),
    ],
    "members": [
        ("member-profile", ["profile", "avatar", "username"]),
        ("member-organizations", ["organization", "team", "workspace"]),
    ],
}

_DEFAULT_CLUSTER_PREFIX = "sub"


class SubFeatureClusterer:
    """
    Groups facts within a feature into granular sub-feature clusters
    using keyword pattern matching on the fact's claim text.

    Tags matching facts with 'sub:<sub-feature-name>' in their tags list
    via returned metadata — actual Fact objects are frozen.
    """

    def cluster(
        self, facts: list[Fact]
    ) -> tuple[dict[str, list[str]], dict[str, str]]:
        """
        Return:
          clusters: {sub_feature_slug -> [fact_id, ...]}
          fact_to_sub: {fact_id -> sub_feature_slug}
        """
        clusters: dict[str, list[str]] = {}
        fact_to_sub: dict[str, str] = {}

        for fact in facts:
            feature = fact.feature.lower()
            patterns = _CLUSTER_PATTERNS.get(feature, [])
            matched_sub: str | None = None

            claim_lower = fact.claim.lower()
            for sub_name, keywords in patterns:
                for kw in keywords:
                    if re.search(kw, claim_lower):
                        matched_sub = sub_name
                        break
                if matched_sub:
                    break

            if not matched_sub:
                # Fallback: use feature itself as the sub-feature
                matched_sub = feature

            clusters.setdefault(matched_sub, []).append(fact.id)
            fact_to_sub[fact.id] = matched_sub

        return clusters, fact_to_sub


# ---------------------------------------------------------------------------
# FactAnalyzer — orchestrator
# ---------------------------------------------------------------------------


class FactAnalyzer:
    """
    Orchestrates the full fact analysis pipeline:
      1. NoiseFilter
      2. FeatureReclassifier
      3. Deduplicator
      4. SubFeatureClusterer

    Public interface for the pipeline (duplicate.py):
        facts = await analyzer.analyze(raw_facts)

    Richer interface (for CLI / reporting):
        report = analyzer.analyze_report(raw_facts)
    """

    def __init__(self, spec_store: SpecStore, keychain: Any = None) -> None:
        self.spec_store = spec_store
        self.keychain = keychain
        self.noise_filter = NoiseFilter()
        self.deduplicator = Deduplicator()
        self.reclassifier = FeatureReclassifier()
        self.clusterer = SubFeatureClusterer()

    def analyze_report(self, all_facts: list[Fact]) -> AnalysisReport:
        """
        Run full analysis pipeline and return an AnalysisReport.

        Facts are not mutated — noise/dedup decisions are tracked by ID sets.
        """
        if not all_facts:
            return AnalysisReport(
                total_facts=0,
                noise_filtered=0,
                deduplicated=0,
                kept=0,
                clusters={},
                facts_by_feature={},
                noise_patterns={},
            )

        # Phase 1: Filter noise
        noise_ids, noise_patterns = self.noise_filter.identify_noise(all_facts)
        clean_facts = [f for f in all_facts if f.id not in noise_ids]

        # Phase 2: Reclassify features (returns dict of fact_id -> new_feature)
        remaps = self.reclassifier.reclassify(clean_facts)
        # Apply remaps to produce a view with corrected features
        reclassified_facts = [
            _with_feature(f, remaps[f.id]) if f.id in remaps else f
            for f in clean_facts
        ]

        # Phase 3: Deduplicate
        dedup_ids = self.deduplicator.identify_duplicates(reclassified_facts)
        unique_facts = [f for f in reclassified_facts if f.id not in dedup_ids]

        # Phase 4: Cluster sub-features
        clusters, _fact_to_sub = self.clusterer.cluster(unique_facts)

        # Build facts_by_feature
        facts_by_feature: dict[str, int] = {}
        for fact in unique_facts:
            facts_by_feature[fact.feature] = facts_by_feature.get(fact.feature, 0) + 1

        return AnalysisReport(
            total_facts=len(all_facts),
            noise_filtered=len(noise_ids),
            deduplicated=len(dedup_ids),
            kept=len(unique_facts),
            clusters=clusters,
            facts_by_feature=facts_by_feature,
            noise_patterns=noise_patterns,
        )

    async def analyze(self, facts: list[Fact]) -> list[Fact]:
        """
        Pipeline interface: run full analysis and return the kept facts list.

        Compatible with duplicate.py's _analyze_facts() which expects list[Fact].
        """
        if not facts:
            return []

        logger.info("Starting fact analysis for %d facts", len(facts))

        report = self.analyze_report(facts)

        logger.info(
            "Curation: %d kept of %d total — %d noise, %d deduped",
            report.kept,
            report.total_facts,
            report.noise_filtered,
            report.deduplicated,
        )
        if report.noise_patterns:
            for rule, count in sorted(report.noise_patterns.items(), key=lambda x: -x[1]):
                logger.debug("  Noise rule '%s': %d facts removed", rule, count)

        # Return the kept facts (reclassified + deduped)
        noise_ids, _ = self.noise_filter.identify_noise(facts)
        clean_facts = [f for f in facts if f.id not in noise_ids]

        remaps = self.reclassifier.reclassify(clean_facts)
        reclassified_facts = [
            _with_feature(f, remaps[f.id]) if f.id in remaps else f
            for f in clean_facts
        ]

        dedup_ids = self.deduplicator.identify_duplicates(reclassified_facts)
        kept = [f for f in reclassified_facts if f.id not in dedup_ids]

        # Tag facts with sub-feature (append to tags copy)
        _, fact_to_sub = self.clusterer.cluster(kept)
        tagged = [_with_sub_tag(f, fact_to_sub.get(f.id)) for f in kept]

        return tagged


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _with_feature(fact: Fact, new_feature: str) -> Fact:
    """Return a new Fact (frozen dataclass rebuild) with a corrected feature."""
    return Fact(
        id=fact.id,
        feature=new_feature,
        category=fact.category,
        claim=fact.claim,
        evidence=fact.evidence,
        source_type=fact.source_type,
        structured_data=fact.structured_data,
        module_name=fact.module_name,
        authority=fact.authority,
        confidence=fact.confidence,
        run_id=fact.run_id,
        status=fact.status,
        verified_by=fact.verified_by,
        contradicted_by=fact.contradicted_by,
        supersedes=fact.supersedes,
        corroborates=fact.corroborates,
        contradicts=fact.contradicts,
        revision=fact.revision,
        deleted_at=fact.deleted_at,
        observed_at=fact.observed_at,
        freshness_ttl_days=fact.freshness_ttl_days,
        tags=fact.tags,
        redaction_status=fact.redaction_status,
        created_at=fact.created_at,
    )


def _with_sub_tag(fact: Fact, sub_feature: str | None) -> Fact:
    """Return a new Fact with the sub-feature tag appended."""
    if not sub_feature:
        return fact
    tag = f"sub:{sub_feature}"
    if tag in fact.tags:
        return fact
    return Fact(
        id=fact.id,
        feature=fact.feature,
        category=fact.category,
        claim=fact.claim,
        evidence=fact.evidence,
        source_type=fact.source_type,
        structured_data=fact.structured_data,
        module_name=fact.module_name,
        authority=fact.authority,
        confidence=fact.confidence,
        run_id=fact.run_id,
        status=fact.status,
        verified_by=fact.verified_by,
        contradicted_by=fact.contradicted_by,
        supersedes=fact.supersedes,
        corroborates=fact.corroborates,
        contradicts=fact.contradicts,
        revision=fact.revision,
        deleted_at=fact.deleted_at,
        observed_at=fact.observed_at,
        freshness_ttl_days=fact.freshness_ttl_days,
        tags=[*fact.tags, tag],
        redaction_status=fact.redaction_status,
        created_at=fact.created_at,
    )
