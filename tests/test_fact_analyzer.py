"""
Tests for scripts/fact_analyzer.py

Covers: NoiseFilter, Deduplicator, FeatureReclassifier,
        SubFeatureClusterer, FactAnalyzer, AnalysisReport.
"""

from __future__ import annotations

import pytest

from scripts.fact_analyzer import (
    Deduplicator,
    FactAnalyzer,
    FeatureReclassifier,
    NoiseFilter,
    SubFeatureClusterer,
)
from scripts.models import (
    Authority,
    EvidenceRef,
    Fact,
    FactCategory,
    SourceType,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def make_fact(
    *,
    feature: str = "boards",
    category: FactCategory = FactCategory.API_ENDPOINT,
    claim: str = "GET /1/boards returns 200",
    source_url: str = "https://trello.com/1/boards",
    source_type: SourceType = SourceType.LIVE_APP,
    authority: Authority = Authority.AUTHORITATIVE,
    structured_data: dict | None = None,
    tags: list[str] | None = None,
) -> Fact:
    return Fact(
        feature=feature,
        category=category,
        claim=claim,
        evidence=[EvidenceRef(source_url=source_url)],
        source_type=source_type,
        authority=authority,
        structured_data=structured_data or {},
        tags=tags or [],
    )


class MockSpecStore:
    """Minimal mock that satisfies FactAnalyzer.__init__."""

    root = None


# ---------------------------------------------------------------------------
# NoiseFilter
# ---------------------------------------------------------------------------


class TestNoiseFilterStaticAssets:
    def test_removes_js(self):
        f = make_fact(
            claim="GET /bundle.js returns 200",
            source_url="https://trello.com/assets/bundle.js",
        )
        nf = NoiseFilter()
        assert nf.is_noise(f) is True

    def test_removes_css(self):
        f = make_fact(
            claim="GET /style.css returns 200",
            source_url="https://trello.com/static/style.css",
        )
        assert NoiseFilter().is_noise(f) is True

    def test_removes_woff(self):
        f = make_fact(
            claim="GET /font.woff2 returns 200",
            source_url="https://trello.com/fonts/font.woff2",
        )
        assert NoiseFilter().is_noise(f) is True

    def test_removes_png(self):
        f = make_fact(
            claim="GET /logo.png returns 200",
            source_url="https://trello.com/images/logo.png",
        )
        assert NoiseFilter().is_noise(f) is True

    def test_removes_map(self):
        f = make_fact(
            claim="GET /app.js.map returns 200",
            source_url="https://trello.com/assets/app.js.map",
        )
        assert NoiseFilter().is_noise(f) is True


class TestNoiseFilterAnalytics:
    def test_removes_gasv3(self):
        f = make_fact(
            claim="POST /gasv3/api/report",
            source_url="https://trello.com/1/gasv3/api/report",
        )
        assert NoiseFilter().is_noise(f) is True

    def test_removes_analytics_path(self):
        f = make_fact(
            claim="POST /analytics/track",
            source_url="https://analytics.example.com/track",
        )
        assert NoiseFilter().is_noise(f) is True

    def test_removes_collect(self):
        f = make_fact(
            claim="POST /collect returns 200",
            source_url="https://google-analytics.com/collect",
        )
        assert NoiseFilter().is_noise(f) is True

    def test_removes_sentry(self):
        f = make_fact(
            claim="POST /sentry/api/12345/envelope/",
            source_url="https://sentry.io/api/12345/envelope/",
        )
        assert NoiseFilter().is_noise(f) is True

    def test_removes_pagead(self):
        f = make_fact(
            claim="GET /pagead/viewthroughconversion",
            source_url="https://ad.doubleclick.net/pagead/viewthroughconversion",
        )
        assert NoiseFilter().is_noise(f) is True


class TestNoiseFilterKeepsApiCalls:
    def test_keeps_trello_boards(self):
        f = make_fact(
            claim="GET /1/boards returns JSON board list",
            source_url="https://trello.com/1/boards",
            structured_data={"path": "/1/boards", "method": "GET"},
        )
        assert NoiseFilter().is_noise(f) is False

    def test_keeps_trello_cards(self):
        f = make_fact(
            feature="cards",
            claim="GET /1/cards/{id} returns card object",
            source_url="https://trello.com/1/cards/abc123",
            structured_data={"path": "/1/cards/abc123", "method": "GET"},
        )
        assert NoiseFilter().is_noise(f) is False

    def test_keeps_help_center_article(self):
        f = make_fact(
            feature="boards",
            category=FactCategory.UI_COMPONENT,
            claim="Board background settings allow custom images",
            source_url="https://support.trello.com/hc/en-us/articles/board-backgrounds",
            source_type=SourceType.HELP_CENTER,
        )
        assert NoiseFilter().is_noise(f) is False

    def test_removes_js_required_help_center(self):
        f = make_fact(
            feature="general",
            category=FactCategory.UI_COMPONENT,
            claim="Please enable JavaScript to view this page",
            source_url="https://support.trello.com/hc/en-us/articles/123",
            source_type=SourceType.HELP_CENTER,
        )
        assert NoiseFilter().is_noise(f) is True


class TestNoiseFilterChunkAssets:
    def test_removes_hash_chunk(self):
        f = make_fact(
            claim="GET /a1b2c3d4.chunk.js returns 200",
            source_url="https://trello.com/assets/a1b2c3d4.chunk.js",
        )
        assert NoiseFilter().is_noise(f) is True

    def test_keeps_api_with_hex_like_id(self):
        # Trello board IDs can look like short hex strings, but path starts with /1/
        f = make_fact(
            claim="GET /1/boards/a1b2c3d4e5f6 returns board",
            source_url="https://trello.com/1/boards/a1b2c3d4e5f6",
            structured_data={"path": "/1/boards/a1b2c3d4e5f6", "method": "GET"},
        )
        assert NoiseFilter().is_noise(f) is False


class TestNoiseFilterIdentifyBatch:
    def test_returns_ids_and_patterns(self):
        nf = NoiseFilter()
        f_noise = make_fact(source_url="https://trello.com/bundle.js")
        f_analytics = make_fact(
            claim="POST /collect",
            source_url="https://example.com/collect",
        )
        f_good = make_fact(
            claim="GET /1/boards returns board list",
            source_url="https://trello.com/1/boards",
        )
        noise_ids, patterns = nf.identify_noise([f_noise, f_analytics, f_good])
        assert f_noise.id in noise_ids
        assert f_analytics.id in noise_ids
        assert f_good.id not in noise_ids
        assert len(patterns) > 0


# ---------------------------------------------------------------------------
# Deduplicator
# ---------------------------------------------------------------------------


class TestDeduplicator:
    def test_dedup_same_api_path_keeps_richer(self):
        d = Deduplicator()
        f1 = make_fact(
            claim="GET /1/boards returns 200",
            structured_data={"path": "/1/boards", "method": "GET"},
            authority=Authority.ANECDOTAL,
        )
        f2 = make_fact(
            claim="GET /1/boards returns board list JSON",
            structured_data={
                "path": "/1/boards",
                "method": "GET",
                "response_body_sample": '{"id": "xyz", "name": "My Board"}',
            },
            authority=Authority.AUTHORITATIVE,
        )
        dedup_ids = d.identify_duplicates([f1, f2])
        # f1 is the worse one; f2 has response body sample
        assert f1.id in dedup_ids
        assert f2.id not in dedup_ids

    def test_dedup_different_paths_not_merged(self):
        d = Deduplicator()
        f1 = make_fact(
            claim="GET /1/boards",
            structured_data={"path": "/1/boards", "method": "GET"},
        )
        f2 = make_fact(
            feature="cards",
            claim="GET /1/cards",
            structured_data={"path": "/1/cards", "method": "GET"},
        )
        dedup_ids = d.identify_duplicates([f1, f2])
        assert len(dedup_ids) == 0

    def test_dedup_normalizes_ids_in_path(self):
        d = Deduplicator()
        f1 = make_fact(
            claim="GET /1/boards/abc123",
            structured_data={"path": "/1/boards/abc12345", "method": "GET"},
        )
        f2 = make_fact(
            claim="GET /1/boards/xyz789",
            structured_data={
                "path": "/1/boards/xyz78901",
                "method": "GET",
                "response_body_sample": '{"id": "xyz789"}',
            },
        )
        dedup_ids = d.identify_duplicates([f1, f2])
        # Both normalize to GET /1/boards/{id}
        assert len(dedup_ids) == 1

    def test_dedup_help_center_by_title(self):
        d = Deduplicator()
        f1 = make_fact(
            feature="boards",
            category=FactCategory.UI_COMPONENT,
            claim="How to create a board",
            source_url="https://support.trello.com/hc/1",
            source_type=SourceType.HELP_CENTER,
            structured_data={"title": "How to Create a Board"},
        )
        f2 = make_fact(
            feature="boards",
            category=FactCategory.UI_COMPONENT,
            claim="How to create a board — Trello Help",
            source_url="https://support.trello.com/hc/2",
            source_type=SourceType.HELP_CENTER,
            structured_data={"title": "How to Create a Board"},
        )
        dedup_ids = d.identify_duplicates([f1, f2])
        assert len(dedup_ids) == 1

    def test_dedup_websocket_events(self):
        d = Deduplicator()
        f1 = make_fact(
            feature="general",
            category=FactCategory.WS_EVENT,
            claim="Ping event sent to server",
            structured_data={"event_name": "ping", "direction": "outgoing"},
        )
        f2 = make_fact(
            feature="general",
            category=FactCategory.WS_EVENT,
            claim="Ping event sent to server (duplicate observation)",
            structured_data={"event_name": "ping", "direction": "outgoing"},
        )
        dedup_ids = d.identify_duplicates([f1, f2])
        assert len(dedup_ids) == 1

    def test_single_fact_not_deduped(self):
        d = Deduplicator()
        f = make_fact(structured_data={"path": "/1/boards", "method": "GET"})
        dedup_ids = d.identify_duplicates([f])
        assert len(dedup_ids) == 0


# ---------------------------------------------------------------------------
# FeatureReclassifier
# ---------------------------------------------------------------------------


class TestFeatureReclassifier:
    def test_reclassify_cards_from_api_path(self):
        r = FeatureReclassifier()
        f = make_fact(
            feature="boards",  # wrong feature
            claim="GET /1/cards/{id} returns card JSON",
            structured_data={"path": "/1/cards/abc123", "method": "GET"},
        )
        remaps = r.reclassify([f])
        assert f.id in remaps
        assert remaps[f.id] == "cards"

    def test_reclassify_lists_from_api_path(self):
        r = FeatureReclassifier()
        f = make_fact(
            feature="boards",
            claim="GET /1/lists/{id} returns list",
            structured_data={"path": "/1/lists/listid", "method": "GET"},
        )
        remaps = r.reclassify([f])
        assert remaps.get(f.id) == "lists"

    def test_reclassify_members_from_api_path(self):
        r = FeatureReclassifier()
        f = make_fact(
            feature="see",  # garbage
            claim="GET /1/members/me returns member",
            structured_data={"path": "/1/members/me", "method": "GET"},
        )
        remaps = r.reclassify([f])
        assert remaps.get(f.id) == "members"

    def test_reclassify_pricing_from_url(self):
        r = FeatureReclassifier()
        f = make_fact(
            feature="free",  # garbage
            claim="Pricing page shows plan tiers",
            source_url="https://trello.com/pricing",
            category=FactCategory.UI_COMPONENT,
            source_type=SourceType.MARKETING,
        )
        remaps = r.reclassify([f])
        assert remaps.get(f.id) == "pricing"

    def test_reclassify_fixes_garbage_feature_see(self):
        r = FeatureReclassifier()
        f = make_fact(
            feature="see",
            claim="Users can see their boards on the home screen",
            category=FactCategory.UI_COMPONENT,
            source_type=SourceType.MARKETING,
        )
        remaps = r.reclassify([f])
        # "see" should be remapped
        assert f.id in remaps
        assert remaps[f.id] != "see"

    def test_reclassify_fixes_garbage_feature_palace(self):
        r = FeatureReclassifier()
        f = make_fact(feature="palace", claim="Something about palace")
        remaps = r.reclassify([f])
        assert f.id in remaps
        assert remaps[f.id] != "palace"

    def test_good_feature_not_remapped(self):
        r = FeatureReclassifier()
        f = make_fact(
            feature="boards",
            claim="GET /1/boards returns board list",
            structured_data={"path": "/1/boards", "method": "GET"},
        )
        remaps = r.reclassify([f])
        assert f.id not in remaps

    def test_auth_from_api_path(self):
        r = FeatureReclassifier()
        f = make_fact(
            feature="boards",
            claim="GET /1/tokens/{token} returns token info",
            structured_data={"path": "/1/tokens/mytoken", "method": "GET"},
        )
        remaps = r.reclassify([f])
        assert remaps.get(f.id) == "auth"


# ---------------------------------------------------------------------------
# SubFeatureClusterer
# ---------------------------------------------------------------------------


class TestSubFeatureClusterer:
    def test_clusters_board_creation(self):
        c = SubFeatureClusterer()
        f = make_fact(
            feature="boards",
            claim="User can create a new board from the home screen",
        )
        clusters, fact_to_sub = c.cluster([f])
        assert fact_to_sub.get(f.id) == "board-creation"
        assert f.id in clusters.get("board-creation", [])

    def test_clusters_board_sharing(self):
        c = SubFeatureClusterer()
        f = make_fact(
            feature="boards",
            claim="User can invite members to a board via email",
        )
        _, fact_to_sub = c.cluster([f])
        assert fact_to_sub.get(f.id) == "board-sharing"

    def test_clusters_card_attachments(self):
        c = SubFeatureClusterer()
        f = make_fact(
            feature="cards",
            claim="Users can attach files to cards via drag and drop",
            structured_data={"path": "/1/cards/{id}/attachments", "method": "POST"},
        )
        _, fact_to_sub = c.cluster([f])
        assert fact_to_sub.get(f.id) == "card-attachments"

    def test_clusters_card_due_dates(self):
        c = SubFeatureClusterer()
        f = make_fact(
            feature="cards",
            claim="Cards support due date and deadline reminders",
        )
        _, fact_to_sub = c.cluster([f])
        assert fact_to_sub.get(f.id) == "card-due-dates"

    def test_unknown_feature_falls_back_to_feature_name(self):
        c = SubFeatureClusterer()
        f = make_fact(
            feature="power-ups",
            claim="Power-ups can be enabled per board",
        )
        _, fact_to_sub = c.cluster([f])
        assert fact_to_sub.get(f.id) == "power-ups"

    def test_produces_cluster_dict(self):
        c = SubFeatureClusterer()
        f1 = make_fact(feature="boards", claim="Create a new board")
        f2 = make_fact(feature="boards", claim="Archive a closed board")
        clusters, _ = c.cluster([f1, f2])
        assert "board-creation" in clusters
        assert "board-archive" in clusters or "board-settings" in clusters


# ---------------------------------------------------------------------------
# FactAnalyzer — full pipeline
# ---------------------------------------------------------------------------


class TestFactAnalyzer:
    def _make_analyzer(self) -> FactAnalyzer:
        return FactAnalyzer(MockSpecStore())  # type: ignore[arg-type]

    def test_full_pipeline_reduces_facts(self):
        """End-to-end: 100 mixed facts → significantly fewer kept."""
        facts = []

        # 60 static assets (should all be filtered)
        for i in range(60):
            facts.append(make_fact(
                claim=f"GET /chunk-{i}.js returns 200",
                source_url=f"https://trello.com/assets/chunk{i:02d}.js",
            ))

        # 20 analytics (should all be filtered)
        for i in range(20):
            facts.append(make_fact(
                claim=f"POST /analytics/track event {i}",
                source_url=f"https://analytics.example.com/track?t={i}",
            ))

        # 10 good API endpoint facts (kept, but 5 are dupes of the same path)
        for i in range(5):
            facts.append(make_fact(
                feature="boards",
                claim=f"GET /1/boards returns board list (observation {i})",
                source_url="https://trello.com/1/boards",
                structured_data={"path": "/1/boards", "method": "GET"},
            ))
        # 5 unique good facts
        for i in range(5):
            facts.append(make_fact(
                feature="cards",
                claim=f"GET /1/cards/{i} returns card object",
                source_url=f"https://trello.com/1/cards/card{i}",
                structured_data={"path": f"/1/cards/card{i}", "method": "GET"},
            ))

        # 10 help-center facts (5 unique, 5 dupes)
        for i in range(5):
            facts.append(make_fact(
                feature="boards",
                category=FactCategory.UI_COMPONENT,
                claim=f"How to create a board — take {i}",
                source_url=f"https://support.trello.com/hc/article/create-board-{i}",
                source_type=SourceType.HELP_CENTER,
                structured_data={"title": "How to Create a Board"},
            ))
        for i in range(5):
            facts.append(make_fact(
                feature="cards",
                category=FactCategory.UI_COMPONENT,
                claim=f"Adding labels to cards explained — version {i}",
                source_url=f"https://support.trello.com/hc/article/labels-{i}",
                source_type=SourceType.HELP_CENTER,
                structured_data={"title": "Adding Labels to Cards"},
            ))

        assert len(facts) == 100

        analyzer = self._make_analyzer()
        report = analyzer.analyze_report(facts)

        # 80 noise facts (60 JS + 20 analytics)
        assert report.noise_filtered == 80
        assert report.total_facts == 100
        # 5 board API dupes + 4 help-center dupes (5 board, 5 cards → 4 board dupes, 4 card dupes)
        assert report.deduplicated >= 4
        assert report.kept < 20
        # Sanity: counts add up
        assert report.kept == report.total_facts - report.noise_filtered - report.deduplicated

    def test_analysis_report_counts_correct(self):
        """Verify report numbers add up: kept = total - noise - deduped."""
        facts = [
            make_fact(
                claim="GET /1/boards returns JSON",
                structured_data={"path": "/1/boards", "method": "GET"},
            ),
            make_fact(
                claim="GET /1/boards returns list (dupe)",
                structured_data={"path": "/1/boards", "method": "GET"},
            ),
            make_fact(
                claim="GET /bundle.js returns 200",
                source_url="https://trello.com/bundle.js",
            ),
        ]
        analyzer = self._make_analyzer()
        report = analyzer.analyze_report(facts)

        assert report.total_facts == 3
        assert report.noise_filtered == 1
        assert report.deduplicated == 1
        assert report.kept == 1
        assert report.kept == report.total_facts - report.noise_filtered - report.deduplicated

    @pytest.mark.asyncio
    async def test_analyze_returns_list_of_facts(self):
        """analyze() pipeline interface returns list[Fact]."""
        analyzer = self._make_analyzer()
        facts = [
            make_fact(
                claim="GET /1/boards returns board list",
                source_url="https://trello.com/1/boards",
                structured_data={"path": "/1/boards", "method": "GET"},
            ),
            make_fact(
                claim="GET /bundle.js",
                source_url="https://trello.com/bundle.js",
            ),
        ]
        result = await analyzer.analyze(facts)
        assert isinstance(result, list)
        assert all(isinstance(f, Fact) for f in result)
        assert len(result) == 1
        assert result[0].claim == "GET /1/boards returns board list"

    @pytest.mark.asyncio
    async def test_analyze_tags_sub_features(self):
        """analyze() should tag kept facts with sub: prefix."""
        analyzer = self._make_analyzer()
        f = make_fact(
            feature="boards",
            claim="User can create a new board from the home screen",
            structured_data={},
        )
        result = await analyzer.analyze([f])
        assert len(result) == 1
        sub_tags = [t for t in result[0].tags if t.startswith("sub:")]
        assert len(sub_tags) == 1
        assert sub_tags[0] == "sub:board-creation"

    @pytest.mark.asyncio
    async def test_analyze_empty_input(self):
        analyzer = self._make_analyzer()
        result = await analyzer.analyze([])
        assert result == []

    def test_analyze_report_empty_input(self):
        analyzer = self._make_analyzer()
        report = analyzer.analyze_report([])
        assert report.total_facts == 0
        assert report.kept == 0

    def test_reclassifier_applied_in_pipeline(self):
        """Facts with garbage features should be reclassified in the output."""
        analyzer = self._make_analyzer()
        f = make_fact(
            feature="see",
            claim="GET /1/members/me returns member profile",
            structured_data={"path": "/1/members/me", "method": "GET"},
        )
        report = analyzer.analyze_report([f])
        # Should be kept and reclassified to "members"
        assert report.kept == 1
        assert "members" in report.facts_by_feature

    def test_noise_patterns_breakdown_in_report(self):
        """Report.noise_patterns should list rule names and counts."""
        analyzer = self._make_analyzer()
        facts = [
            make_fact(source_url="https://trello.com/a.js"),
            make_fact(source_url="https://trello.com/b.css"),
            make_fact(
                claim="POST /collect",
                source_url="https://analytics.com/collect",
            ),
        ]
        report = analyzer.analyze_report(facts)
        assert report.noise_filtered == 3
        # All three should be in noise_patterns
        total_removed = sum(report.noise_patterns.values())
        assert total_removed == 3

    def test_clusters_in_report(self):
        """Report.clusters should group fact_ids by sub-feature slug."""
        analyzer = self._make_analyzer()
        # Use UI_COMPONENT category so deduplicator doesn't collapse them as API endpoints
        f1 = make_fact(
            feature="boards",
            category=FactCategory.UI_COMPONENT,
            claim="User can create a new board from the home screen",
            source_url="https://trello.com/new-board",
            source_type=SourceType.HELP_CENTER,
        )
        f2 = make_fact(
            feature="boards",
            category=FactCategory.UI_COMPONENT,
            claim="User can invite members to their board via the share dialog",
            source_url="https://trello.com/board-settings",
            source_type=SourceType.HELP_CENTER,
        )
        report = analyzer.analyze_report([f1, f2])
        assert "board-creation" in report.clusters
        assert "board-sharing" in report.clusters
        assert f1.id in report.clusters["board-creation"]
        assert f2.id in report.clusters["board-sharing"]
