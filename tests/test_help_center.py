"""
Tests for scripts/recon/help_center.py — HelpCenterModule.

All HTTP responses are mocked — no real network calls.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx

from scripts.models import Authority, FactCategory, SourceType
from scripts.recon.base import (
    ReconModuleStatus,
    ReconRequest,
    ReconResult,
    ReconServices,
)
from scripts.recon.help_center import (
    HelpCenterModule,
    _article_category,
    _extract_article_text,
    _extract_related_features,
    _infer_feature_from_url_and_title,
)

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


def _mock_response(
    status_code: int = 200,
    text: str = "",
    content_type: str = "text/html",
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = {"content-type": content_type}
    return resp


# ---------------------------------------------------------------------------
# Sample HTML fixtures
# ---------------------------------------------------------------------------

HELP_ARTICLE_HTML = """
<!DOCTYPE html>
<html>
<head><title>How to create a board | Trello Support</title></head>
<body>
  <article>
    <h1>How to create a board</h1>
    <p>Boards are the foundation of Trello. Follow these steps to create a new board:</p>
    <ol>
      <li>Click the "+" button in the top navigation bar.</li>
      <li>Select "Create Board" from the dropdown menu.</li>
      <li>Enter a name for your Board and choose a background.</li>
      <li>Click the "Create Board" button to finish.</li>
    </ol>
    <p>You can also invite team members to collaborate on your Board immediately.</p>
  </article>
</body>
</html>
"""

UI_ARTICLE_HTML = """
<!DOCTYPE html>
<html>
<head><title>The Trello Sidebar | Trello Support</title></head>
<body>
  <main>
    <h1>The Trello Sidebar</h1>
    <p>The sidebar panel provides quick access to your boards, workspaces, and settings.
    Use the toolbar icon to toggle the sidebar open or closed. The dashboard shows all
    recent boards. You can pin the sidebar to keep it visible at all times.</p>
  </main>
</body>
</html>
"""

HELP_INDEX_HTML = """
<!DOCTYPE html>
<html>
<head><title>Trello Help Center</title></head>
<body>
  <nav>
    <a href="/help/articles/boards">Boards</a>
    <a href="/help/articles/cards">Cards</a>
    <a href="/help/articles/lists">Lists</a>
    <a href="https://other-domain.com/help">External</a>
  </nav>
</body>
</html>
"""

SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://trello.com/help/articles/boards</loc></url>
  <url><loc>https://trello.com/help/articles/cards</loc></url>
  <url><loc>https://other-domain.com/page</loc></url>
</urlset>
"""

SITEMAP_INDEX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://trello.com/sitemap-help.xml</loc></sitemap>
</sitemapindex>
"""

EMPTY_HTML = "<html><body><p>.</p></body></html>"


# ---------------------------------------------------------------------------
# Module property tests
# ---------------------------------------------------------------------------


class TestModuleProperties:
    def test_name(self) -> None:
        mod = HelpCenterModule()
        assert mod.name == "help_center"

    def test_authority(self) -> None:
        mod = HelpCenterModule()
        assert mod.authority == Authority.OBSERVATIONAL

    def test_source_type(self) -> None:
        mod = HelpCenterModule()
        assert mod.source_type == SourceType.HELP_CENTER

    def test_requires_credentials_empty(self) -> None:
        mod = HelpCenterModule()
        assert mod.requires_credentials == []

    def test_validate_prerequisites_returns_empty(self) -> None:
        mod = HelpCenterModule()
        result = _run(mod.validate_prerequisites())
        assert result == []


# ---------------------------------------------------------------------------
# Article category classification
# ---------------------------------------------------------------------------


class TestArticleCategory:
    def test_how_to_article_is_user_flow(self) -> None:
        result = _article_category("How to create a board", "Follow these steps")
        assert result == FactCategory.USER_FLOW

    def test_guide_article_is_user_flow(self) -> None:
        assert _article_category("Getting started guide", "") == FactCategory.USER_FLOW

    def test_ui_component_article(self) -> None:
        result = _article_category("The Sidebar", "The sidebar panel shows buttons and menus.")
        assert result == FactCategory.UI_COMPONENT

    def test_flow_keyword_takes_priority_over_ui(self) -> None:
        # "how to" in title wins over "button" in body
        result = _article_category("How to use the button", "Click the button")
        assert result == FactCategory.USER_FLOW

    def test_default_is_user_flow(self) -> None:
        result = _article_category("Random Title", "Some neutral text about things.")
        assert result == FactCategory.USER_FLOW


# ---------------------------------------------------------------------------
# Feature inference
# ---------------------------------------------------------------------------


class TestInferFeature:
    def test_scope_feature_matched_from_title(self) -> None:
        f = _infer_feature_from_url_and_title(
            "https://trello.com/help/boards", "Boards Overview", ["boards", "cards"]
        )
        assert f == "boards"

    def test_scope_feature_matched_from_url_segment(self) -> None:
        f = _infer_feature_from_url_and_title(
            "https://trello.com/help/articles/cards", "Card Features", ["boards", "cards"]
        )
        assert f == "cards"

    def test_generic_segment_used_when_no_scope(self) -> None:
        f = _infer_feature_from_url_and_title(
            "https://trello.com/help/articles/checklists", "Checklists", []
        )
        assert f == "checklists"

    def test_skips_help_segment(self) -> None:
        f = _infer_feature_from_url_and_title(
            "https://trello.com/help/automation", "Automation", []
        )
        assert f == "automation"

    def test_default_when_no_segments_uses_title_slug(self) -> None:
        # No path segments → falls back to first word of title
        f = _infer_feature_from_url_and_title("https://trello.com/", "Trello", [])
        assert f == "trello"

    def test_default_when_no_segments_and_no_title(self) -> None:
        # No path segments and empty title → "help-center"
        f = _infer_feature_from_url_and_title("https://trello.com/", "", [])
        assert f == "help-center"


# ---------------------------------------------------------------------------
# Article text extraction
# ---------------------------------------------------------------------------


class TestExtractArticleText:
    def test_extracts_from_article_element(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(HELP_ARTICLE_HTML, "html.parser")
        text = _extract_article_text(soup)
        assert "create a new board" in text

    def test_extracts_from_main_element(self) -> None:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(UI_ARTICLE_HTML, "html.parser")
        text = _extract_article_text(soup)
        assert "sidebar" in text.lower()

    def test_falls_back_to_body_text(self) -> None:
        from bs4 import BeautifulSoup

        html = "<html><body><p>Some plain body text about the product.</p></body></html>"
        soup = BeautifulSoup(html, "html.parser")
        text = _extract_article_text(soup)
        assert "body text" in text


# ---------------------------------------------------------------------------
# Related feature extraction
# ---------------------------------------------------------------------------


class TestExtractRelatedFeatures:
    def test_extracts_capitalised_nouns(self) -> None:
        text = "The Board contains Lists and Cards. You can also use Checklists."
        features = _extract_related_features(text)
        assert len(features) > 0

    def test_returns_at_most_five(self) -> None:
        text = "Use Alpha Beta Gamma Delta Epsilon Zeta Eta features."
        features = _extract_related_features(text)
        assert len(features) <= 5

    def test_deduplicates(self) -> None:
        text = "Board Board Board is a thing."
        features = _extract_related_features(text)
        assert features.count("board") <= 1

    def test_empty_text_returns_empty_list(self) -> None:
        features = _extract_related_features("")
        assert features == []


# ---------------------------------------------------------------------------
# Sitemap parsing tests
# ---------------------------------------------------------------------------


class TestSitemapParsing:
    def _sync_fetch(self, mod: HelpCenterModule, client: Any, url: str, domain: str) -> list[str]:
        return _run(mod._fetch_sitemap(client, url, domain))

    def test_parses_sitemap_xml(self) -> None:
        mod = HelpCenterModule()
        mock_client = AsyncMock()
        sitemap_resp = _mock_response(200, SITEMAP_XML, "application/xml")
        mock_client.get = AsyncMock(return_value=sitemap_resp)
        urls = self._sync_fetch(mod, mock_client, "https://trello.com/sitemap.xml", "trello.com")
        assert "https://trello.com/help/articles/boards" in urls
        assert "https://trello.com/help/articles/cards" in urls

    def test_filters_out_of_domain_urls(self) -> None:
        mod = HelpCenterModule()
        mock_client = AsyncMock()
        sitemap_resp = _mock_response(200, SITEMAP_XML, "application/xml")
        mock_client.get = AsyncMock(return_value=sitemap_resp)
        urls = self._sync_fetch(mod, mock_client, "https://trello.com/sitemap.xml", "trello.com")
        assert "https://other-domain.com/page" not in urls

    def test_returns_empty_on_404(self) -> None:
        mod = HelpCenterModule()
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_response(404))
        urls = self._sync_fetch(mod, mock_client, "https://trello.com/sitemap.xml", "trello.com")
        assert urls == []

    def test_returns_empty_on_invalid_xml(self) -> None:
        mod = HelpCenterModule()
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=_mock_response(200, "NOT XML", "text/plain"))
        urls = self._sync_fetch(mod, mock_client, "https://trello.com/sitemap.xml", "trello.com")
        assert urls == []

    def test_handles_network_error(self) -> None:
        mod = HelpCenterModule()
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.RequestError("Connection refused"))
        urls = self._sync_fetch(mod, mock_client, "https://trello.com/sitemap.xml", "trello.com")
        assert urls == []

    def test_sitemap_index_recurses(self) -> None:
        mod = HelpCenterModule()
        sub_sitemap = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://trello.com/help/boards</loc></url>
        </urlset>"""
        call_count = 0

        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if "sitemap-help" in url:
                return _mock_response(200, sub_sitemap, "application/xml")
            return _mock_response(200, SITEMAP_INDEX_XML, "application/xml")

        mock_client = AsyncMock()
        mock_client.get = mock_get
        urls = self._sync_fetch(mod, mock_client, "https://trello.com/sitemap.xml", "trello.com")
        assert "https://trello.com/help/boards" in urls



# ---------------------------------------------------------------------------
# Index crawling tests
# ---------------------------------------------------------------------------


class TestIndexCrawling:
    def test_crawl_collects_same_domain_links(self) -> None:
        mod = HelpCenterModule()
        call_count = 0

        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_response(200, HELP_INDEX_HTML, "text/html")
            return _mock_response(200, HELP_ARTICLE_HTML, "text/html")

        mock_client = AsyncMock()
        mock_client.get = mock_get
        urls, errors = _run(
            mod._crawl_index(mock_client, "https://trello.com/help", "trello.com", {})
        )
        assert "https://trello.com/help" in urls
        # Should have followed internal links
        assert len(urls) > 1

    def test_crawl_excludes_external_links(self) -> None:
        mod = HelpCenterModule()

        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            return _mock_response(200, HELP_INDEX_HTML, "text/html")

        mock_client = AsyncMock()
        mock_client.get = mock_get
        urls, _ = _run(
            mod._crawl_index(mock_client, "https://trello.com/help", "trello.com", {})
        )
        for u in urls:
            assert "other-domain.com" not in u

    def test_crawl_respects_max_pages_budget(self) -> None:
        mod = HelpCenterModule()
        link_html = '<html><body>' + ''.join(
            f'<a href="/help/page-{i}">Page {i}</a>' for i in range(50)
        ) + '</body></html>'

        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            return _mock_response(200, link_html, "text/html")

        mock_client = AsyncMock()
        mock_client.get = mock_get
        urls, _ = _run(
            mod._crawl_index(
                mock_client, "https://trello.com/help", "trello.com", {"max_pages": 3}
            )
        )
        assert len(urls) <= 3


# ---------------------------------------------------------------------------
# Page to Fact conversion
# ---------------------------------------------------------------------------


class TestPageToFact:
    def test_produces_fact_from_article(self) -> None:
        mod = HelpCenterModule()
        fact = mod._page_to_fact(HELP_ARTICLE_HTML, "https://trello.com/help/boards", "run-1", [])
        assert fact is not None

    def test_fact_authority_is_observational(self) -> None:
        mod = HelpCenterModule()
        fact = mod._page_to_fact(HELP_ARTICLE_HTML, "https://trello.com/help/boards", "run-1", [])
        assert fact is not None
        assert fact.authority == Authority.OBSERVATIONAL

    def test_fact_source_type_is_help_center(self) -> None:
        mod = HelpCenterModule()
        fact = mod._page_to_fact(HELP_ARTICLE_HTML, "https://trello.com/help/boards", "run-1", [])
        assert fact is not None
        assert fact.source_type == SourceType.HELP_CENTER

    def test_fact_module_name_is_help_center(self) -> None:
        mod = HelpCenterModule()
        fact = mod._page_to_fact(HELP_ARTICLE_HTML, "https://trello.com/help/boards", "run-1", [])
        assert fact is not None
        assert fact.module_name == "help_center"

    def test_fact_has_at_least_one_evidence(self) -> None:
        mod = HelpCenterModule()
        fact = mod._page_to_fact(HELP_ARTICLE_HTML, "https://trello.com/help/boards", "run-1", [])
        assert fact is not None
        assert len(fact.evidence) >= 1

    def test_fact_evidence_source_url_matches(self) -> None:
        mod = HelpCenterModule()
        url = "https://trello.com/help/boards"
        fact = mod._page_to_fact(HELP_ARTICLE_HTML, url, "run-1", [])
        assert fact is not None
        assert fact.evidence[0].source_url == url

    def test_fact_run_id_set(self) -> None:
        mod = HelpCenterModule()
        fact = mod._page_to_fact(HELP_ARTICLE_HTML, "https://trello.com/help/boards", "run-99", [])
        assert fact is not None
        assert fact.run_id == "run-99"

    def test_fact_category_user_flow_for_howto(self) -> None:
        mod = HelpCenterModule()
        fact = mod._page_to_fact(HELP_ARTICLE_HTML, "https://trello.com/help/boards", "run-1", [])
        assert fact is not None
        assert fact.category == FactCategory.USER_FLOW

    def test_fact_category_ui_component_for_ui_article(self) -> None:
        mod = HelpCenterModule()
        fact = mod._page_to_fact(UI_ARTICLE_HTML, "https://trello.com/help/sidebar", "run-1", [])
        assert fact is not None
        assert fact.category == FactCategory.UI_COMPONENT

    def test_fact_claim_contains_title(self) -> None:
        mod = HelpCenterModule()
        fact = mod._page_to_fact(HELP_ARTICLE_HTML, "https://trello.com/help/boards", "run-1", [])
        assert fact is not None
        assert "create a board" in fact.claim.lower()

    def test_returns_none_for_empty_page(self) -> None:
        mod = HelpCenterModule()
        fact = mod._page_to_fact(EMPTY_HTML, "https://trello.com/help/empty", "run-1", [])
        assert fact is None

    def test_fact_structured_data_has_title(self) -> None:
        mod = HelpCenterModule()
        fact = mod._page_to_fact(HELP_ARTICLE_HTML, "https://trello.com/help/boards", "run-1", [])
        assert fact is not None
        assert "title" in fact.structured_data
        assert "create a board" in fact.structured_data["title"].lower()

    def test_fact_structured_data_has_related_features(self) -> None:
        mod = HelpCenterModule()
        fact = mod._page_to_fact(HELP_ARTICLE_HTML, "https://trello.com/help/boards", "run-1", [])
        assert fact is not None
        assert "related_features" in fact.structured_data

    def test_fact_feature_matched_from_scope(self) -> None:
        mod = HelpCenterModule()
        fact = mod._page_to_fact(
            HELP_ARTICLE_HTML,
            "https://trello.com/help/boards",
            "run-1",
            ["boards", "cards"],
        )
        assert fact is not None
        assert fact.feature == "boards"

    def test_title_suffix_stripped(self) -> None:
        mod = HelpCenterModule()
        fact = mod._page_to_fact(HELP_ARTICLE_HTML, "https://trello.com/help/boards", "run-1", [])
        assert fact is not None
        # "| Trello Support" should be stripped from the title in the claim
        assert "Trello Support" not in fact.claim


# ---------------------------------------------------------------------------
# run() integration tests (INV-020)
# ---------------------------------------------------------------------------


class TestRunIntegration:
    def _sync_run(
        self, mod: HelpCenterModule, request: ReconRequest, services: ReconServices
    ) -> ReconResult:
        return asyncio.run(mod.run(request, services))

    def test_run_returns_result_on_network_failure(self) -> None:
        mod = HelpCenterModule()
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Network down"))
        mock_client.aclose = AsyncMock()
        result = self._sync_run(mod, _make_request(), _make_services(mock_client))
        assert isinstance(result, ReconResult)

    def test_run_module_name_matches(self) -> None:
        mod = HelpCenterModule()
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Network down"))
        mock_client.aclose = AsyncMock()
        result = self._sync_run(mod, _make_request(), _make_services(mock_client))
        assert result.module == "help_center"

    def test_run_status_failed_when_no_facts(self) -> None:
        mod = HelpCenterModule()
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Network down"))
        mock_client.aclose = AsyncMock()
        result = self._sync_run(mod, _make_request(), _make_services(mock_client))
        assert result.status == ReconModuleStatus.FAILED
        assert result.facts == []

    def test_run_produces_facts_from_sitemap_and_articles(self) -> None:
        mod = HelpCenterModule()

        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            if "sitemap.xml" in url:
                return _mock_response(200, SITEMAP_XML, "application/xml")
            if "sitemap" in url:
                return _mock_response(404)
            return _mock_response(200, HELP_ARTICLE_HTML, "text/html")

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.aclose = AsyncMock()

        request = _make_request(
            module_config={"sitemap_url": "https://trello.com/sitemap.xml"}
        )
        result = self._sync_run(mod, request, _make_services(mock_client))
        assert result.module == "help_center"
        assert len(result.facts) > 0
        assert result.status in (ReconModuleStatus.SUCCESS, ReconModuleStatus.PARTIAL)

    def test_run_facts_have_correct_authority(self) -> None:
        mod = HelpCenterModule()

        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            if "sitemap" in url:
                return _mock_response(404)
            return _mock_response(200, HELP_ARTICLE_HTML, "text/html")

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.aclose = AsyncMock()

        request = _make_request(
            module_config={"help_url": "https://trello.com/help"}
        )
        result = self._sync_run(mod, request, _make_services(mock_client))
        for fact in result.facts:
            assert fact.authority == Authority.OBSERVATIONAL

    def test_run_facts_have_correct_source_type(self) -> None:
        mod = HelpCenterModule()

        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            if "sitemap" in url:
                return _mock_response(404)
            return _mock_response(200, HELP_ARTICLE_HTML, "text/html")

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.aclose = AsyncMock()

        request = _make_request(
            module_config={"help_url": "https://trello.com/help"}
        )
        result = self._sync_run(mod, request, _make_services(mock_client))
        for fact in result.facts:
            assert fact.source_type == SourceType.HELP_CENTER

    def test_run_facts_module_name_set(self) -> None:
        mod = HelpCenterModule()

        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            if "sitemap" in url:
                return _mock_response(404)
            return _mock_response(200, HELP_ARTICLE_HTML, "text/html")

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.aclose = AsyncMock()

        request = _make_request(
            module_config={"help_url": "https://trello.com/help"}
        )
        result = self._sync_run(mod, request, _make_services(mock_client))
        for fact in result.facts:
            assert fact.module_name == "help_center"

    def test_run_progress_callback_called(self) -> None:
        mod = HelpCenterModule()
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Network down"))
        mock_client.aclose = AsyncMock()

        progress_events: list[Any] = []
        asyncio.run(
            mod.run(
                _make_request(),
                _make_services(mock_client),
                progress=lambda p: progress_events.append(p),
            )
        )
        assert len(progress_events) >= 1

    def test_run_partial_when_some_pages_fail(self) -> None:
        mod = HelpCenterModule()
        call_count = 0

        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if "sitemap.xml" in url:
                return _mock_response(200, SITEMAP_XML, "application/xml")
            if call_count % 2 == 0:
                return _mock_response(200, HELP_ARTICLE_HTML, "text/html")
            return _mock_response(503)  # server error for some pages

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.aclose = AsyncMock()

        request = _make_request(
            module_config={"sitemap_url": "https://trello.com/sitemap.xml"}
        )
        result = self._sync_run(mod, request, _make_services(mock_client))
        assert isinstance(result, ReconResult)
        assert result.module == "help_center"

    def test_run_rate_limited_captured_not_raised(self) -> None:
        mod = HelpCenterModule()

        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            resp = _mock_response(429)
            resp.headers = {"content-type": "text/html", "Retry-After": "0"}
            return resp

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.aclose = AsyncMock()

        request = _make_request(
            module_config={"help_url": "https://trello.com/help"}
        )
        result = self._sync_run(mod, request, _make_services(mock_client))
        assert isinstance(result, ReconResult)
        assert result.status in (ReconModuleStatus.FAILED, ReconModuleStatus.PARTIAL)

    def test_run_metrics_populated(self) -> None:
        mod = HelpCenterModule()
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Network down"))
        mock_client.aclose = AsyncMock()
        result = self._sync_run(mod, _make_request(), _make_services(mock_client))
        assert "articles_found" in result.metrics
        assert "errors" in result.metrics
        assert "urls_visited" in result.metrics
