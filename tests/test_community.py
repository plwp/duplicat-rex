"""
Tests for scripts/recon/community.py — CommunityModule.

All HTTP responses are mocked — no real network calls.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from scripts.models import Authority, Confidence, FactCategory, SourceType
from scripts.recon.base import (
    ReconModuleStatus,
    ReconRequest,
    ReconResult,
    ReconServices,
)
from scripts.recon.community import CommunityModule

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(**kwargs: Any) -> ReconRequest:
    defaults: dict[str, Any] = {
        "run_id": "test-run-id",
        "target": "trello.com",
        "base_url": "https://trello.com",
    }
    defaults.update(kwargs)
    return ReconRequest(**defaults)


def _make_services(http_client: Any = None) -> ReconServices:
    return ReconServices(
        spec_store=None,
        credentials={},
        artifact_store=None,
        http_client=http_client,
        browser=None,
        clock=None,
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _make_reddit_post(
    title: str = "Trello is missing X feature",
    selftext: str = "I wish trello had this workaround",
    subreddit: str = "trello",
    score: int = 50,
    num_comments: int = 10,
    permalink: str = "/r/trello/comments/abc/test/",
    created_utc: float = 1700000000.0,
) -> dict[str, Any]:
    return {
        "title": title,
        "selftext": selftext,
        "subreddit": subreddit,
        "score": score,
        "num_comments": num_comments,
        "permalink": permalink,
        "url": f"https://www.reddit.com{permalink}",
        "created_utc": created_utc,
    }


def _make_listing(posts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "data": {
            "children": [{"kind": "t3", "data": p} for p in posts]
        }
    }


def _mock_json_response(
    data: Any,
    status_code: int = 200,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    resp.headers = {}
    return resp


def _mock_client(responses: list[MagicMock]) -> MagicMock:
    """Build a mock async HTTP client that returns responses in sequence."""
    client = MagicMock()
    client.get = AsyncMock(side_effect=responses)
    client.aclose = AsyncMock()
    return client


# ---------------------------------------------------------------------------
# Module property tests
# ---------------------------------------------------------------------------


class TestModuleProperties:
    def test_name(self) -> None:
        mod = CommunityModule()
        assert mod.name == "community"

    def test_authority(self) -> None:
        mod = CommunityModule()
        assert mod.authority == Authority.ANECDOTAL

    def test_source_type(self) -> None:
        mod = CommunityModule()
        assert mod.source_type == SourceType.COMMUNITY

    def test_requires_credentials(self) -> None:
        mod = CommunityModule()
        assert mod.requires_credentials == []


# ---------------------------------------------------------------------------
# _extract_target_name
# ---------------------------------------------------------------------------


class TestExtractTargetName:
    def setup_method(self) -> None:
        self.mod = CommunityModule()

    def test_strips_tld(self) -> None:
        assert self.mod._extract_target_name("trello.com") == "trello"

    def test_strips_scheme(self) -> None:
        assert self.mod._extract_target_name("https://asana.com") == "asana"

    def test_subdomain_stripped(self) -> None:
        # first segment only
        assert self.mod._extract_target_name("app.trello.com") == "app"

    def test_bare_name_passthrough(self) -> None:
        assert self.mod._extract_target_name("notion") == "notion"


# ---------------------------------------------------------------------------
# _build_subreddit_list
# ---------------------------------------------------------------------------


class TestBuildSubredditList:
    def setup_method(self) -> None:
        self.mod = CommunityModule()

    def test_default_includes_target_and_bases(self) -> None:
        subs = self.mod._build_subreddit_list("trello", {})
        assert "trello" in subs
        assert "productivity" in subs
        assert "projectmanagement" in subs

    def test_config_override(self) -> None:
        subs = self.mod._build_subreddit_list("trello", {"subreddits": ["r1", "r2"]})
        assert subs == ["r1", "r2"]


# ---------------------------------------------------------------------------
# _filter_high_signal
# ---------------------------------------------------------------------------


class TestFilterHighSignal:
    def setup_method(self) -> None:
        self.mod = CommunityModule()

    def test_passes_signal_post(self) -> None:
        posts = [_make_reddit_post(title="Trello bug: cards disappearing", score=10)]
        result = self.mod._filter_high_signal(posts)
        assert len(result) == 1

    def test_blocks_noise_post(self) -> None:
        posts = [_make_reddit_post(title="[Hiring] Trello admin wanted", score=100)]
        result = self.mod._filter_high_signal(posts)
        assert len(result) == 0

    def test_blocks_low_score(self) -> None:
        posts = [_make_reddit_post(title="Trello workaround trick", score=1)]
        result = self.mod._filter_high_signal(posts)
        assert len(result) == 0

    def test_body_signal_is_enough(self) -> None:
        posts = [
            _make_reddit_post(
                title="Question about Trello",
                selftext="I found a workaround for this annoying issue",
                score=5,
            )
        ]
        result = self.mod._filter_high_signal(posts)
        assert len(result) == 1

    def test_no_signal_keyword_filtered(self) -> None:
        posts = [
            _make_reddit_post(
                title="What is everyone using Trello for?",
                selftext="Just curious about your setups.",
                score=20,
            )
        ]
        result = self.mod._filter_high_signal(posts)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# _infer_feature
# ---------------------------------------------------------------------------


class TestInferFeature:
    def setup_method(self) -> None:
        self.mod = CommunityModule()

    def test_boards_keyword(self) -> None:
        assert self.mod._infer_feature("Kanban boards not loading", "", "trello") == "boards"

    def test_notifications(self) -> None:
        result = self.mod._infer_feature("Email notifications broken", "", "trello")
        assert result == "notifications"

    def test_api_keyword(self) -> None:
        # "webhook" matches "integrations" first (integrations pattern includes webhook)
        assert self.mod._infer_feature("Webhook stopped working", "", "trello") == "integrations"

    def test_api_keyword_direct(self) -> None:
        assert self.mod._infer_feature("Trello REST API rate limit", "", "trello") == "api"

    def test_fallback(self) -> None:
        result = self.mod._infer_feature("Random question about stuff", "", "trello")
        assert result == "community-feedback"


# ---------------------------------------------------------------------------
# _post_to_fact
# ---------------------------------------------------------------------------


class TestPostToFact:
    def setup_method(self) -> None:
        self.mod = CommunityModule()

    def test_basic_fact_shape(self) -> None:
        post = _make_reddit_post()
        fact = self.mod._post_to_fact(post, "run-1", "trello")

        assert fact.module_name == "community"
        assert fact.authority == Authority.ANECDOTAL
        assert fact.source_type == SourceType.COMMUNITY
        assert fact.run_id == "run-1"
        assert len(fact.evidence) == 1
        assert "trello" in fact.evidence[0].source_url

    def test_high_score_gives_medium_confidence(self) -> None:
        post = _make_reddit_post(score=100, num_comments=50)
        fact = self.mod._post_to_fact(post, "run-1", "trello")
        assert fact.confidence == Confidence.MEDIUM

    def test_low_score_gives_low_confidence(self) -> None:
        post = _make_reddit_post(score=5, num_comments=2)
        fact = self.mod._post_to_fact(post, "run-1", "trello")
        assert fact.confidence == Confidence.LOW

    def test_workaround_gives_user_flow_category(self) -> None:
        post = _make_reddit_post(
            title="Workaround for Trello sync issue",
            selftext="Step 1: do this. Step 2: do that.",
        )
        fact = self.mod._post_to_fact(post, "run-1", "trello")
        assert fact.category == FactCategory.USER_FLOW

    def test_complaint_gives_business_rule_category(self) -> None:
        post = _make_reddit_post(
            title="Trello is terrible at permissions",
            selftext="The admin controls are broken and frustrating.",
        )
        fact = self.mod._post_to_fact(post, "run-1", "trello")
        assert fact.category == FactCategory.BUSINESS_RULE

    def test_evidence_has_published_at(self) -> None:
        post = _make_reddit_post(created_utc=1700000000.0)
        fact = self.mod._post_to_fact(post, "run-1", "trello")
        assert fact.evidence[0].published_at is not None

    def test_structured_data_contains_subreddit(self) -> None:
        post = _make_reddit_post(subreddit="productivity")
        fact = self.mod._post_to_fact(post, "run-1", "trello")
        assert fact.structured_data["subreddit"] == "productivity"


# ---------------------------------------------------------------------------
# run() — integration tests with mocked HTTP
# ---------------------------------------------------------------------------


class TestRun:
    def setup_method(self) -> None:
        self.mod = CommunityModule()

    def test_run_does_not_raise_on_network_failure(self) -> None:
        """INV-020: run() MUST NOT raise."""
        import httpx

        client = MagicMock()
        client.get = AsyncMock(side_effect=httpx.RequestError("connection refused"))
        client.aclose = AsyncMock()

        services = _make_services(http_client=client)
        request = _make_request()
        result = _run(self.mod.run(request, services))

        assert isinstance(result, ReconResult)
        assert result.module == "community"
        assert result.status == ReconModuleStatus.FAILED

    def test_run_returns_facts_on_success(self) -> None:
        post = _make_reddit_post(title="Trello cards bug is frustrating", score=30)
        listing = _make_listing([post])

        # One response per subreddit (trello, productivity, projectmanagement)
        responses = [
            _mock_json_response(listing),
            _mock_json_response(listing),
            _mock_json_response(listing),
        ]
        client = _mock_client(responses)
        services = _make_services(http_client=client)
        request = _make_request()

        result = _run(self.mod.run(request, services))

        assert result.module == "community"
        assert result.status in (ReconModuleStatus.SUCCESS, ReconModuleStatus.PARTIAL)
        assert len(result.facts) > 0
        assert all(f.authority == Authority.ANECDOTAL for f in result.facts)
        assert all(f.module_name == "community" for f in result.facts)
        assert all(f.source_type == SourceType.COMMUNITY for f in result.facts)
        assert all(len(f.evidence) >= 1 for f in result.facts)

    def test_run_handles_rate_limit(self) -> None:
        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.headers = {"Retry-After": "0"}

        ok_post = _make_reddit_post(title="I hate Trello bugs", score=10)
        ok_listing = _make_listing([ok_post])
        ok_response = _mock_json_response(ok_listing)

        # First subreddit gets rate-limited (exhaust backoff), second two succeed
        responses = (
            [rate_limited] * 5  # 1 initial + 4 retries for first subreddit
            + [ok_response, ok_response]
        )
        client = _mock_client(responses)
        services = _make_services(http_client=client)
        request = _make_request()

        result = _run(self.mod.run(request, services))

        assert isinstance(result, ReconResult)
        # Should have errors for the rate-limited sub but still produce facts
        assert len(result.errors) > 0
        assert len(result.facts) > 0

    def test_run_handles_404_gracefully(self) -> None:
        """404 for non-existent subreddit should not produce an error."""
        not_found = MagicMock()
        not_found.status_code = 404
        not_found.headers = {}

        ok_post = _make_reddit_post(title="Trello workaround", score=10)
        ok_listing = _make_listing([ok_post])
        ok_response = _mock_json_response(ok_listing)

        client = _mock_client([not_found, ok_response, ok_response])
        services = _make_services(http_client=client)
        request = _make_request()

        result = _run(self.mod.run(request, services))

        assert isinstance(result, ReconResult)
        assert result.module == "community"
        # No error for 404 (subreddit just doesn't exist)
        assert len(result.errors) == 0

    def test_run_failed_when_no_signal_posts(self) -> None:
        """No high-signal posts → FAILED status."""
        boring_post = _make_reddit_post(
            title="What is everyone using?",
            selftext="Just curious.",
            score=5,
        )
        listing = _make_listing([boring_post])
        responses = [_mock_json_response(listing)] * 3
        client = _mock_client(responses)
        services = _make_services(http_client=client)
        request = _make_request()

        result = _run(self.mod.run(request, services))

        assert result.status == ReconModuleStatus.FAILED
        assert len(result.facts) == 0

    def test_run_progress_callback_called(self) -> None:
        post = _make_reddit_post(title="Trello bug is terrible", score=20)
        listing = _make_listing([post])
        responses = [_mock_json_response(listing)] * 3
        client = _mock_client(responses)
        services = _make_services(http_client=client)
        request = _make_request()

        progress_events: list[Any] = []
        _run(self.mod.run(request, services, progress=progress_events.append))

        phases = [e.phase for e in progress_events]
        assert "init" in phases
        assert "complete" in phases

    def test_run_module_config_subreddits_override(self) -> None:
        """module_config["subreddits"] limits which subreddits are queried."""
        post = _make_reddit_post(title="Trello hate the slow cards", score=10)
        listing = _make_listing([post])
        ok_response = _mock_json_response(listing)

        client = _mock_client([ok_response])  # Only one call expected
        services = _make_services(http_client=client)
        request = _make_request(module_config={"subreddits": ["customsub"]})

        result = _run(self.mod.run(request, services))

        assert result.module == "community"
        assert client.get.call_count == 1

    def test_run_result_module_name_invariant(self) -> None:
        """ENSURES: ReconResult.module == self.name."""
        responses = [_mock_json_response(_make_listing([]))] * 3
        client = _mock_client(responses)
        services = _make_services(http_client=client)
        request = _make_request()

        result = _run(self.mod.run(request, services))
        assert result.module == "community"
