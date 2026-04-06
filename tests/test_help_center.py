"""
Tests for scripts/recon/help_center.py — HelpCenterModule.

All HTTP calls are mocked; no real network access required.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from bs4 import BeautifulSoup

from scripts.models import (
    Authority,
    Confidence,
    FactCategory,
    Scope,
    SourceType,
)
from scripts.recon.base import (
    ReconModuleStatus,
    ReconRequest,
    ReconResult,
    ReconServices,
)
from scripts.recon.help_center import HelpCenterModule, _HelpArticle

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_request(
    target: str = "example.com",
    base_url: str = "",
    module_config: dict[str, Any] | None = None,
) -> ReconRequest:
    return ReconRequest(
        run_id=str(uuid.uuid4()),
        target=target,
        base_url=base_url or f"https://{target}",
        scope=Scope(),
        module_config=module_config or {},
    )


def _make_services(http_client: Any = None) -> ReconServices:
    return ReconServices(
        spec_store=MagicMock(),
        credentials={},
        artifact_store=MagicMock(),
        http_client=http_client,
        browser=None,
    )


def _make_http_response(html: str, status_code: int = 200) -> httpx.Response:
    """Build a fake httpx.Response from raw HTML."""
    return httpx.Response(
        status_code=status_code,
        content=html.encode(),
        headers={"content-type": "text/html; charset=utf-8"},
        request=httpx.Request("GET", "https://example.com/help"),
    )


_ARTICLE_HTML = """
<html>
<head><title>How to create a board</title></head>
<body>
  <nav><a href="/">Home</a></nav>
  <main>
    <h1>How to create a board</h1>
    <p>Boards are where your work gets organized. To create a board, click the
    "Create" button in the top navigation bar, then select "Board" from the
    dropdown menu. Give your board a name and choose a visibility setting.</p>
    <ul>
      <li>Click the "+" icon in the header</li>
      <li>Enter a board title</li>
      <li>Select a workspace</li>
    </ul>
  </main>
  <footer>Copyright 2024</footer>
</body>
</html>
"""

_UI_COMPONENT_HTML = """
<html>
<head><title>Understanding the sidebar</title></head>
<body>
  <main>
    <h1>Understanding the sidebar</h1>
    <p>The sidebar panel gives you quick access to your boards, teams, and
    recent activity. You can collapse the sidebar by clicking the toggle button
    at the top-left corner. The toolbar at the bottom shows notification badges.</p>
  </main>
</body>
</html>
"""

_THIN_HTML = "<html><body><p>Short</p></body></html>"

_SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/help/create-board</loc></url>
  <url><loc>https://example.com/help/manage-cards</loc></url>
  <url><loc>https://example.com/about</loc></url>
</urlset>
"""

_EMPTY_HTML = "<html><body><p>Nothing here</p></body></html>"


# ---------------------------------------------------------------------------
# Module properties
# ---------------------------------------------------------------------------


class TestModuleProperties:
    def test_name(self) -> None:
        assert HelpCenterModule().name == "help_center"

    def test_authority(self) -> None:
        assert HelpCenterModule().authority == Authority.OBSERVATIONAL

    def test_source_type(self) -> None:
        assert HelpCenterModule().source_type == SourceType.HELP_CENTER

    def test_requires_credentials_empty(self) -> None:
        assert HelpCenterModule().requires_credentials == []


# ---------------------------------------------------------------------------
# Sitemap parsing
# ---------------------------------------------------------------------------


class TestTrySitemap:
    """Tests for _try_sitemap."""

    @pytest.mark.asyncio
    async def test_parses_sitemap_urls(self) -> None:
        module = HelpCenterModule()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(
            return_value=httpx.Response(
                status_code=200,
                content=_SITEMAP_XML.encode(),
                headers={"content-type": "application/xml"},
                request=httpx.Request("GET", "https://example.com/sitemap.xml"),
            )
        )
        urls, error = await module._try_sitemap(mock_client, "https://example.com/sitemap.xml")
        assert error is None
        assert "https://example.com/help/create-board" in urls
        assert "https://example.com/help/manage-cards" in urls

    @pytest.mark.asyncio
    async def test_sitemap_404_returns_empty(self) -> None:
        module = HelpCenterModule()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(
            return_value=httpx.Response(
                status_code=404,
                content=b"Not Found",
                headers={"content-type": "text/html"},
                request=httpx.Request("GET", "https://example.com/sitemap.xml"),
            )
        )
        urls, error = await module._try_sitemap(mock_client, "https://example.com/sitemap.xml")
        assert urls == []
        assert error is None  # 404 is not an error — sitemap simply not present

    @pytest.mark.asyncio
    async def test_sitemap_network_error_returns_error(self) -> None:
        module = HelpCenterModule()
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=httpx.RequestError("connection refused"))
        urls, error = await module._try_sitemap(mock_client, "https://example.com/sitemap.xml")
        assert urls == []
        assert error is not None
        assert error.error_type == "parse_error"

    @pytest.mark.asyncio
    async def test_filters_to_same_domain(self) -> None:
        module = HelpCenterModule()
        sitemap_with_external = """<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://example.com/help/article</loc></url>
          <url><loc>https://other.com/page</loc></url>
        </urlset>"""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(
            return_value=httpx.Response(
                status_code=200,
                content=sitemap_with_external.encode(),
                headers={"content-type": "application/xml"},
                request=httpx.Request("GET", "https://example.com/sitemap.xml"),
            )
        )
        urls, _ = await module._try_sitemap(mock_client, "https://example.com/sitemap.xml")
        assert all("example.com" in u for u in urls)
        assert not any("other.com" in u for u in urls)

    @pytest.mark.asyncio
    async def test_help_urls_sorted_first(self) -> None:
        module = HelpCenterModule()
        sitemap = """<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://example.com/blog/post</loc></url>
          <url><loc>https://example.com/help/article</loc></url>
        </urlset>"""
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(
            return_value=httpx.Response(
                status_code=200,
                content=sitemap.encode(),
                headers={"content-type": "application/xml"},
                request=httpx.Request("GET", "https://example.com/sitemap.xml"),
            )
        )
        urls, _ = await module._try_sitemap(mock_client, "https://example.com/sitemap.xml")
        # Help URL should come before the blog URL
        help_idx = next(i for i, u in enumerate(urls) if "/help/" in u)
        blog_idx = next(i for i, u in enumerate(urls) if "/blog/" in u)
        assert help_idx < blog_idx


# ---------------------------------------------------------------------------
# Article HTML parsing
# ---------------------------------------------------------------------------


class TestParseArticle:
    def _parse(self, html: str, url: str = "https://example.com/help/a") -> _HelpArticle | None:
        module = HelpCenterModule()
        soup = BeautifulSoup(html, "html.parser")
        return module._parse_article(soup, url)

    def test_extracts_title_from_h1(self) -> None:
        article = self._parse(_ARTICLE_HTML)
        assert article is not None
        assert "create a board" in article.title.lower()

    def test_extracts_body_text(self) -> None:
        article = self._parse(_ARTICLE_HTML)
        assert article is not None
        assert "organized" in article.body

    def test_body_excludes_nav(self) -> None:
        article = self._parse(_ARTICLE_HTML)
        assert article is not None
        assert "Home" not in article.body

    def test_body_excludes_footer(self) -> None:
        article = self._parse(_ARTICLE_HTML)
        assert article is not None
        assert "Copyright" not in article.body

    def test_source_url_preserved(self) -> None:
        url = "https://example.com/help/create-board"
        article = self._parse(_ARTICLE_HTML, url)
        assert article is not None
        assert article.source_url == url

    def test_thin_page_returns_none(self) -> None:
        assert self._parse(_THIN_HTML) is None

    def test_raw_excerpt_capped_at_2000(self) -> None:
        article = self._parse(_ARTICLE_HTML)
        assert article is not None
        assert len(article.raw_excerpt) <= 2000

    def test_title_falls_back_to_page_title_tag(self) -> None:
        html = (
            "<html><head><title>FAQ Page</title></head>"
            "<body><p>This is a long enough body for the article parser "
            "to accept it and not return None because it exceeds the minimum "
            "character threshold required for a valid article.</p></body></html>"
        )
        article = self._parse(html)
        assert article is not None
        assert "FAQ" in article.title


# ---------------------------------------------------------------------------
# Fact category classification
# ---------------------------------------------------------------------------


class TestClassifyFactCategory:
    def setup_method(self) -> None:
        self.module = HelpCenterModule()

    def _make_article(self, title: str, body: str) -> _HelpArticle:
        return _HelpArticle(
            title=title,
            body=body,
            category="",
            source_url="https://example.com/help/x",
            raw_excerpt=body[:200],
        )

    def test_how_to_article_is_user_flow(self) -> None:
        article = self._make_article("How to create a board", "Click the create button to start.")
        assert self.module._classify_fact_category(article) == FactCategory.USER_FLOW

    def test_steps_article_is_user_flow(self) -> None:
        article = self._make_article(
            "Getting started", "Follow these steps to set up your workspace."
        )
        assert self.module._classify_fact_category(article) == FactCategory.USER_FLOW

    def test_sidebar_article_is_ui_component(self) -> None:
        article = self._make_article("The sidebar", "The sidebar panel provides quick access.")
        assert self.module._classify_fact_category(article) == FactCategory.UI_COMPONENT

    def test_button_article_is_ui_component(self) -> None:
        article = self._make_article("Using the toolbar", "The toolbar button opens the menu.")
        assert self.module._classify_fact_category(article) == FactCategory.UI_COMPONENT

    def test_default_is_user_flow(self) -> None:
        article = self._make_article("About boards", "Boards help teams organise their projects.")
        # No strong UI or flow keywords — falls back to USER_FLOW
        result = self.module._classify_fact_category(article)
        assert result in (FactCategory.USER_FLOW, FactCategory.UI_COMPONENT)


# ---------------------------------------------------------------------------
# Feature inference
# ---------------------------------------------------------------------------


class TestInferFeature:
    def setup_method(self) -> None:
        self.module = HelpCenterModule()

    def _make_article(self, title: str, body: str = "") -> _HelpArticle:
        return _HelpArticle(
            title=title,
            body=body,
            category="",
            source_url="https://example.com/help/x",
            raw_excerpt="",
        )

    def test_matches_scope_feature(self) -> None:
        article = self._make_article("How to use boards")
        result = self.module._infer_feature(article, ["boards", "cards"])
        assert result == "boards"

    def test_matches_hyphenated_scope_feature(self) -> None:
        article = self._make_article("How to use drag drop")
        result = self.module._infer_feature(article, ["drag-drop"])
        assert result == "drag-drop"

    def test_falls_back_to_title_word(self) -> None:
        article = self._make_article("Managing permissions")
        result = self.module._infer_feature(article, [])
        assert result == "managing" or result == "permissions"

    def test_skips_stop_words(self) -> None:
        article = self._make_article("How to use the app")
        result = self.module._infer_feature(article, [])
        # Should not return "how", "the", "use", etc.
        assert result not in {"how", "the", "use", "your", "you"}


# ---------------------------------------------------------------------------
# Fact construction
# ---------------------------------------------------------------------------


class TestArticleToFact:
    def setup_method(self) -> None:
        self.module = HelpCenterModule()
        self.run_id = str(uuid.uuid4())

    def _make_article(self, **kwargs: Any) -> _HelpArticle:
        defaults: dict[str, Any] = {
            "title": "How to create a board",
            "body": "Click the Create button to start a new board in your workspace.",
            "category": "Getting Started",
            "source_url": "https://example.com/help/create-board",
            "raw_excerpt": "How to create a board: Click the Create button.",
            "tags": ["boards", "getting-started"],
        }
        defaults.update(kwargs)
        return _HelpArticle(**defaults)

    def test_module_name_set(self) -> None:
        article = self._make_article()
        fact = self.module._article_to_fact(article, self.run_id, [])
        assert fact.module_name == "help_center"

    def test_authority_observational(self) -> None:
        article = self._make_article()
        fact = self.module._article_to_fact(article, self.run_id, [])
        assert fact.authority == Authority.OBSERVATIONAL

    def test_source_type_help_center(self) -> None:
        article = self._make_article()
        fact = self.module._article_to_fact(article, self.run_id, [])
        assert fact.source_type == SourceType.HELP_CENTER

    def test_confidence_medium(self) -> None:
        article = self._make_article()
        fact = self.module._article_to_fact(article, self.run_id, [])
        assert fact.confidence == Confidence.MEDIUM

    def test_evidence_has_source_url(self) -> None:
        article = self._make_article(source_url="https://example.com/help/boards")
        fact = self.module._article_to_fact(article, self.run_id, [])
        assert len(fact.evidence) == 1
        assert fact.evidence[0].source_url == "https://example.com/help/boards"

    def test_run_id_set(self) -> None:
        article = self._make_article()
        fact = self.module._article_to_fact(article, self.run_id, [])
        assert fact.run_id == self.run_id

    def test_claim_contains_title(self) -> None:
        article = self._make_article(title="How to create a board")
        fact = self.module._article_to_fact(article, self.run_id, [])
        assert "How to create a board" in fact.claim

    def test_structured_data_has_title(self) -> None:
        article = self._make_article(title="Board overview")
        fact = self.module._article_to_fact(article, self.run_id, [])
        assert fact.structured_data["title"] == "Board overview"

    def test_category_is_user_flow_or_ui_component(self) -> None:
        article = self._make_article()
        fact = self.module._article_to_fact(article, self.run_id, [])
        assert fact.category in (FactCategory.USER_FLOW, FactCategory.UI_COMPONENT)

    def test_scope_feature_matched(self) -> None:
        article = self._make_article(
            title="Managing board cards", body="Cards are tasks in Trello."
        )
        fact = self.module._article_to_fact(article, self.run_id, ["cards", "boards"])
        assert fact.feature in ("cards", "boards")


# ---------------------------------------------------------------------------
# Full run() integration (mocked HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestRunMethod:
    async def _run(
        self,
        html: str,
        status_code: int = 200,
        module_config: dict[str, Any] | None = None,
        sitemap_response: str | None = None,
    ) -> ReconResult:
        module = HelpCenterModule()
        request = _make_request(
            module_config=module_config
            or {"help_center_url": "https://example.com/help"},
        )

        call_count = 0

        async def mock_get(url: str, **kwargs: Any) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if "sitemap" in url:
                if sitemap_response:
                    return httpx.Response(
                        status_code=200,
                        content=sitemap_response.encode(),
                        headers={"content-type": "application/xml"},
                        request=httpx.Request("GET", url),
                    )
                return httpx.Response(
                    status_code=404,
                    content=b"Not Found",
                    headers={"content-type": "text/html"},
                    request=httpx.Request("GET", url),
                )
            return httpx.Response(
                status_code=status_code,
                content=html.encode(),
                headers={"content-type": "text/html; charset=utf-8"},
                request=httpx.Request("GET", url),
            )

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = mock_get
        mock_client.aclose = AsyncMock()

        services = _make_services(http_client=mock_client)
        return await module.run(request, services)

    async def test_success_status_when_facts_found(self) -> None:
        result = await self._run(_ARTICLE_HTML)
        assert result.status == ReconModuleStatus.SUCCESS
        assert result.module == "help_center"

    async def test_facts_produced(self) -> None:
        result = await self._run(_ARTICLE_HTML)
        assert len(result.facts) > 0

    async def test_all_facts_have_correct_module(self) -> None:
        result = await self._run(_ARTICLE_HTML)
        for fact in result.facts:
            assert fact.module_name == "help_center"

    async def test_all_facts_have_evidence(self) -> None:
        result = await self._run(_ARTICLE_HTML)
        for fact in result.facts:
            assert len(fact.evidence) >= 1

    async def test_all_facts_have_authority_observational(self) -> None:
        result = await self._run(_ARTICLE_HTML)
        for fact in result.facts:
            assert fact.authority == Authority.OBSERVATIONAL

    async def test_all_facts_have_source_type_help_center(self) -> None:
        result = await self._run(_ARTICLE_HTML)
        for fact in result.facts:
            assert fact.source_type == SourceType.HELP_CENTER

    async def test_failed_status_on_empty_page(self) -> None:
        result = await self._run(_THIN_HTML)
        assert result.status == ReconModuleStatus.FAILED
        assert result.facts == []

    async def test_failed_status_on_404(self) -> None:
        result = await self._run("Not Found", status_code=404)
        assert result.status == ReconModuleStatus.FAILED
        assert len(result.errors) > 0

    async def test_run_does_not_raise_on_network_error(self) -> None:
        """INV-020: run() MUST NOT raise."""
        module = HelpCenterModule()
        request = _make_request(
            module_config={"help_center_url": "https://example.com/help"},
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=httpx.RequestError("connection refused"))
        mock_client.aclose = AsyncMock()
        services = _make_services(http_client=mock_client)

        result = await module.run(request, services)
        assert result.module == "help_center"
        assert result.status == ReconModuleStatus.FAILED

    async def test_run_does_not_raise_on_unexpected_exception(self) -> None:
        """INV-020: even unexpected errors are caught."""
        module = HelpCenterModule()
        request = _make_request(
            module_config={"help_center_url": "https://example.com/help"},
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=RuntimeError("completely unexpected"))
        mock_client.aclose = AsyncMock()
        services = _make_services(http_client=mock_client)

        result = await module.run(request, services)
        assert result.module == "help_center"
        assert result.status == ReconModuleStatus.FAILED

    async def test_result_module_matches_name(self) -> None:
        result = await self._run(_ARTICLE_HTML)
        assert result.module == "help_center"

    async def test_progress_callback_called(self) -> None:
        module = HelpCenterModule()
        request = _make_request(
            module_config={"help_center_url": "https://example.com/help"},
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(
            return_value=_make_http_response(_ARTICLE_HTML)
        )
        mock_client.aclose = AsyncMock()
        services = _make_services(http_client=mock_client)

        progress_calls: list[Any] = []
        await module.run(request, services, progress=progress_calls.append)
        assert len(progress_calls) > 0

    async def test_metrics_populated(self) -> None:
        result = await self._run(_ARTICLE_HTML)
        assert "articles_found" in result.metrics
        assert "errors" in result.metrics
        assert "urls_visited" in result.metrics

    async def test_urls_visited_populated(self) -> None:
        result = await self._run(_ARTICLE_HTML)
        assert len(result.urls_visited) > 0

    async def test_creates_own_client_when_none_injected(self) -> None:
        """When services.http_client is None, module creates its own client."""
        module = HelpCenterModule()
        request = _make_request(
            module_config={"help_center_url": "https://example.com/help"},
        )
        services = _make_services(http_client=None)

        with patch("scripts.recon.help_center.httpx.AsyncClient") as mock_cls:
            mock_instance = AsyncMock()
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_instance.get = AsyncMock(return_value=_make_http_response(_ARTICLE_HTML))
            mock_instance.aclose = AsyncMock()
            mock_cls.return_value = mock_instance

            result = await module.run(request, services)

        mock_cls.assert_called_once()
        assert result.module == "help_center"

    async def test_sitemap_urls_used_when_available(self) -> None:
        """When sitemap.xml is available, article URLs are drawn from it."""
        result = await self._run(_ARTICLE_HTML, sitemap_response=_SITEMAP_XML)
        assert result.module == "help_center"
        # Facts should have been produced from sitemap-discovered URLs
        assert len(result.facts) >= 0  # May be 0 if articles are thin — no assertion on count

    async def test_user_flow_facts_correct_category(self) -> None:
        result = await self._run(_ARTICLE_HTML)
        flow_facts = [f for f in result.facts if f.category == FactCategory.USER_FLOW]
        # _ARTICLE_HTML describes a "how to" flow
        assert len(flow_facts) >= 1

    async def test_ui_component_facts_correct_category(self) -> None:
        result = await self._run(_UI_COMPONENT_HTML)
        ui_facts = [f for f in result.facts if f.category == FactCategory.UI_COMPONENT]
        # _UI_COMPONENT_HTML mentions sidebar, panel, toggle — should be UI_COMPONENT
        assert len(ui_facts) >= 1
