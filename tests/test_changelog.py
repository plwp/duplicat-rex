"""
Tests for scripts/recon/changelog.py — ChangelogModule.

All HTTP calls are mocked; no network access required.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
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
from scripts.recon.changelog import ChangelogModule, _ChangeEntry

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


def _make_http_response(
    html: str, status_code: int = 200
) -> httpx.Response:
    """Build a fake httpx.Response from raw HTML."""
    return httpx.Response(
        status_code=status_code,
        content=html.encode(),
        headers={"content-type": "text/html; charset=utf-8"},
        request=httpx.Request("GET", "https://example.com/changelog"),
    )


_SIMPLE_CHANGELOG_HTML = """
<html>
<body>
  <h2>v2.0.0 – January 15, 2024</h2>
  <ul>
    <li>Added new board view for cards</li>
    <li>Fixed login timeout bug</li>
  </ul>
  <h2>v1.9.0 – December 1, 2023</h2>
  <ul>
    <li>Deprecated legacy API endpoints</li>
    <li>Removed old export feature</li>
  </ul>
</body>
</html>
"""

_EMPTY_HTML = "<html><body><p>Nothing here</p></body></html>"


# ---------------------------------------------------------------------------
# Module properties
# ---------------------------------------------------------------------------


class TestModuleProperties:
    def test_name(self) -> None:
        assert ChangelogModule().name == "changelog"

    def test_authority(self) -> None:
        assert ChangelogModule().authority == Authority.ANECDOTAL

    def test_source_type(self) -> None:
        assert ChangelogModule().source_type == SourceType.CHANGELOG

    def test_requires_credentials_empty(self) -> None:
        assert ChangelogModule().requires_credentials == []


# ---------------------------------------------------------------------------
# URL discovery
# ---------------------------------------------------------------------------


class TestDiscoverUrls:
    def test_explicit_url_takes_priority(self) -> None:
        module = ChangelogModule()
        urls = module._discover_urls(
            "https://example.com",
            {"changelog_url": "https://example.com/custom-log"},
        )
        assert urls == ["https://example.com/custom-log"]

    def test_probes_well_known_paths(self) -> None:
        module = ChangelogModule()
        urls = module._discover_urls("https://example.com", {})
        assert any("/changelog" in u for u in urls)
        assert any("/release-notes" in u for u in urls)
        assert any("/whats-new" in u for u in urls)

    def test_base_url_used(self) -> None:
        module = ChangelogModule()
        urls = module._discover_urls("https://docs.example.com", {})
        assert all(u.startswith("https://docs.example.com") for u in urls)


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------


class TestParseChangelogPage:
    def _parse(self, html: str, url: str = "https://example.com/changelog") -> list[_ChangeEntry]:
        module = ChangelogModule()
        soup = BeautifulSoup(html, "html.parser")
        return module._parse_changelog_page(soup, url)

    def test_extracts_bullet_entries(self) -> None:
        entries = self._parse(_SIMPLE_CHANGELOG_HTML)
        assert len(entries) >= 4  # 2 bullets under each heading

    def test_entry_titles_contain_heading(self) -> None:
        entries = self._parse(_SIMPLE_CHANGELOG_HTML)
        titles = [e.title for e in entries]
        assert any("v2.0.0" in t for t in titles)
        assert any("v1.9.0" in t for t in titles)

    def test_empty_page_returns_no_entries(self) -> None:
        entries = self._parse(_EMPTY_HTML)
        assert entries == []

    def test_source_url_preserved(self) -> None:
        url = "https://example.com/release-notes"
        entries = self._parse(_SIMPLE_CHANGELOG_HTML, url)
        assert all(e.source_url == url for e in entries)

    def test_no_nav_noise(self) -> None:
        html = """
        <html><body>
          <nav><a href="/">Home</a></nav>
          <h2>v1.0.0 – 2024-01-01</h2>
          <ul><li>Added feature X</li></ul>
          <footer>Copyright 2024</footer>
        </body></html>
        """
        entries = self._parse(html)
        # nav/footer content should not appear in entries
        for e in entries:
            assert "Home" not in e.description
            assert "Copyright" not in e.description


# ---------------------------------------------------------------------------
# Change type classification
# ---------------------------------------------------------------------------


class TestClassifyChangeType:
    def setup_method(self) -> None:
        self.module = ChangelogModule()

    def test_addition(self) -> None:
        assert self.module._classify_change_type("Added new card view") == "addition"

    def test_removal(self) -> None:
        assert self.module._classify_change_type("Removed the old export") == "removal"

    def test_fix(self) -> None:
        assert self.module._classify_change_type("Fixed login timeout bug") == "fix"

    def test_deprecation(self) -> None:
        assert self.module._classify_change_type("Deprecated legacy endpoints") == "deprecation"

    def test_general_fallback(self) -> None:
        assert self.module._classify_change_type("Some update to the system") == "general"

    def test_deprecation_wins_over_removal(self) -> None:
        # "will be removed" is a deprecation signal
        result = self.module._classify_change_type("This feature will be removed in v3")
        assert result == "deprecation"


# ---------------------------------------------------------------------------
# Date extraction
# ---------------------------------------------------------------------------


class TestExtractDate:
    def setup_method(self) -> None:
        self.module = ChangelogModule()

    def test_iso_date(self) -> None:
        assert self.module._extract_date("Release 2024-03-15") == "2024-03-15"

    def test_us_date(self) -> None:
        result = self.module._extract_date("January 15, 2024")
        assert result == "2024-01-15"

    def test_eu_date(self) -> None:
        result = self.module._extract_date("15 March 2024")
        assert result == "2024-03-15"

    def test_no_date(self) -> None:
        assert self.module._extract_date("No date here") is None

    def test_abbreviated_month(self) -> None:
        result = self.module._extract_date("Jan 5, 2024")
        assert result == "2024-01-05"


# ---------------------------------------------------------------------------
# Recency check
# ---------------------------------------------------------------------------


class TestIsRecent:
    def setup_method(self) -> None:
        self.module = ChangelogModule()

    def test_recent_date_is_recent(self) -> None:
        recent = (datetime.now(UTC) - timedelta(days=5)).date().isoformat()
        assert self.module._is_recent(recent) is True

    def test_old_date_not_recent(self) -> None:
        old = (datetime.now(UTC) - timedelta(days=60)).date().isoformat()
        assert self.module._is_recent(old) is False

    def test_none_not_recent(self) -> None:
        assert self.module._is_recent(None) is False

    def test_boundary_not_recent(self) -> None:
        boundary = (datetime.now(UTC) - timedelta(days=31)).date().isoformat()
        assert self.module._is_recent(boundary) is False


# ---------------------------------------------------------------------------
# Fact construction
# ---------------------------------------------------------------------------


class TestEntryToFact:
    def setup_method(self) -> None:
        self.module = ChangelogModule()
        self.run_id = str(uuid.uuid4())

    def _make_entry(self, **kwargs: Any) -> _ChangeEntry:
        defaults: dict[str, Any] = {
            "title": "v1.0.0",
            "description": "Added new feature",
            "change_type": "addition",
            "published_at": None,
            "source_url": "https://example.com/changelog",
            "raw_excerpt": "v1.0.0: Added new feature",
            "feature_hint": "feature",
            "tags": [],
        }
        defaults.update(kwargs)
        return _ChangeEntry(**defaults)

    def test_module_name_set(self) -> None:
        entry = self._make_entry()
        fact = self.module._entry_to_fact(entry, self.run_id, [])
        assert fact.module_name == "changelog"

    def test_authority_anecdotal(self) -> None:
        entry = self._make_entry()
        fact = self.module._entry_to_fact(entry, self.run_id, [])
        assert fact.authority == Authority.ANECDOTAL

    def test_source_type_changelog(self) -> None:
        entry = self._make_entry()
        fact = self.module._entry_to_fact(entry, self.run_id, [])
        assert fact.source_type == SourceType.CHANGELOG

    def test_evidence_has_source_url(self) -> None:
        entry = self._make_entry(source_url="https://example.com/notes")
        fact = self.module._entry_to_fact(entry, self.run_id, [])
        assert len(fact.evidence) == 1
        assert fact.evidence[0].source_url == "https://example.com/notes"

    def test_recent_entry_gets_medium_confidence(self) -> None:
        recent_date = (datetime.now(UTC) - timedelta(days=5)).date().isoformat()
        entry = self._make_entry(published_at=recent_date)
        fact = self.module._entry_to_fact(entry, self.run_id, [])
        assert fact.confidence == Confidence.MEDIUM

    def test_old_entry_gets_low_confidence(self) -> None:
        old_date = (datetime.now(UTC) - timedelta(days=60)).date().isoformat()
        entry = self._make_entry(published_at=old_date)
        fact = self.module._entry_to_fact(entry, self.run_id, [])
        assert fact.confidence == Confidence.LOW

    def test_addition_gets_business_rule_category(self) -> None:
        entry = self._make_entry(change_type="addition")
        fact = self.module._entry_to_fact(entry, self.run_id, [])
        assert fact.category == FactCategory.BUSINESS_RULE

    def test_fix_gets_configuration_category(self) -> None:
        entry = self._make_entry(change_type="fix")
        fact = self.module._entry_to_fact(entry, self.run_id, [])
        assert fact.category == FactCategory.CONFIGURATION

    def test_structured_data_contains_change_type(self) -> None:
        entry = self._make_entry(change_type="removal")
        fact = self.module._entry_to_fact(entry, self.run_id, [])
        assert fact.structured_data["change_type"] == "removal"

    def test_run_id_set(self) -> None:
        entry = self._make_entry()
        fact = self.module._entry_to_fact(entry, self.run_id, [])
        assert fact.run_id == self.run_id

    def test_scope_feature_matched(self) -> None:
        entry = self._make_entry(
            title="v2.0.0", description="Added drag-drop support for cards"
        )
        fact = self.module._entry_to_fact(entry, self.run_id, ["drag-drop", "boards"])
        assert fact.feature == "drag-drop"


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
    ) -> ReconResult:
        module = ChangelogModule()
        request = _make_request(
            module_config=module_config or {"changelog_url": "https://example.com/changelog"},
        )

        mock_response = _make_http_response(html, status_code)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        services = _make_services(http_client=mock_client)
        return await module.run(request, services)

    async def test_success_status_when_facts_found(self) -> None:
        result = await self._run(_SIMPLE_CHANGELOG_HTML)
        assert result.status == ReconModuleStatus.SUCCESS
        assert result.module == "changelog"

    async def test_facts_produced(self) -> None:
        result = await self._run(_SIMPLE_CHANGELOG_HTML)
        assert len(result.facts) > 0

    async def test_all_facts_have_correct_module(self) -> None:
        result = await self._run(_SIMPLE_CHANGELOG_HTML)
        for fact in result.facts:
            assert fact.module_name == "changelog"

    async def test_all_facts_have_evidence(self) -> None:
        result = await self._run(_SIMPLE_CHANGELOG_HTML)
        for fact in result.facts:
            assert len(fact.evidence) >= 1

    async def test_all_facts_have_authority_anecdotal(self) -> None:
        result = await self._run(_SIMPLE_CHANGELOG_HTML)
        for fact in result.facts:
            assert fact.authority == Authority.ANECDOTAL

    async def test_all_facts_have_source_type_changelog(self) -> None:
        result = await self._run(_SIMPLE_CHANGELOG_HTML)
        for fact in result.facts:
            assert fact.source_type == SourceType.CHANGELOG

    async def test_failed_status_on_empty_page(self) -> None:
        result = await self._run(_EMPTY_HTML)
        assert result.status == ReconModuleStatus.FAILED
        assert result.facts == []

    async def test_failed_status_on_404(self) -> None:
        result = await self._run("Not Found", status_code=404)
        assert result.status == ReconModuleStatus.FAILED
        assert len(result.errors) > 0

    async def test_run_does_not_raise_on_network_error(self) -> None:
        """INV-020: run() MUST NOT raise."""
        module = ChangelogModule()
        request = _make_request(
            module_config={"changelog_url": "https://example.com/changelog"},
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=httpx.RequestError("connection refused"))
        mock_client.aclose = AsyncMock()
        services = _make_services(http_client=mock_client)

        # Must not raise
        result = await module.run(request, services)
        assert result.module == "changelog"
        assert result.status == ReconModuleStatus.FAILED

    async def test_run_does_not_raise_on_unexpected_exception(self) -> None:
        """INV-020: even unexpected errors are caught."""
        module = ChangelogModule()
        request = _make_request(
            module_config={"changelog_url": "https://example.com/changelog"},
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=RuntimeError("completely unexpected"))
        mock_client.aclose = AsyncMock()
        services = _make_services(http_client=mock_client)

        result = await module.run(request, services)
        assert result.module == "changelog"
        assert result.status == ReconModuleStatus.FAILED

    async def test_result_module_matches_name(self) -> None:
        result = await self._run(_SIMPLE_CHANGELOG_HTML)
        assert result.module == "changelog"

    async def test_partial_status_when_some_errors(self) -> None:
        """If facts were extracted but some errors occurred, status is PARTIAL."""
        module = ChangelogModule()
        request = _make_request(
            module_config={"changelog_url": "https://example.com/changelog"},
        )

        call_count = 0

        async def side_effect(*args: Any, **kwargs: Any) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_http_response(_SIMPLE_CHANGELOG_HTML)
            raise httpx.RequestError("second call fails")

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=side_effect)
        mock_client.aclose = AsyncMock()
        services = _make_services(http_client=mock_client)

        result = await module.run(request, services)
        # First call succeeded and produced facts
        assert len(result.facts) > 0

    async def test_progress_callback_called(self) -> None:
        module = ChangelogModule()
        request = _make_request(
            module_config={"changelog_url": "https://example.com/changelog"},
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=_make_http_response(_SIMPLE_CHANGELOG_HTML))
        mock_client.aclose = AsyncMock()
        services = _make_services(http_client=mock_client)

        progress_calls: list[Any] = []
        await module.run(request, services, progress=progress_calls.append)
        assert len(progress_calls) > 0

    async def test_metrics_populated(self) -> None:
        result = await self._run(_SIMPLE_CHANGELOG_HTML)
        assert "entries_found" in result.metrics
        assert "errors" in result.metrics
        assert "urls_visited" in result.metrics

    async def test_urls_visited_populated(self) -> None:
        result = await self._run(_SIMPLE_CHANGELOG_HTML)
        assert len(result.urls_visited) > 0

    async def test_creates_own_client_when_none_injected(self) -> None:
        """When services.http_client is None, module creates its own client."""
        module = ChangelogModule()
        request = _make_request(
            module_config={"changelog_url": "https://example.com/changelog"},
        )
        services = _make_services(http_client=None)

        with patch("scripts.recon.changelog.httpx.AsyncClient") as mock_cls:
            mock_instance = AsyncMock()
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_instance.get = AsyncMock(
                return_value=_make_http_response(_SIMPLE_CHANGELOG_HTML)
            )
            mock_instance.aclose = AsyncMock()
            mock_cls.return_value = mock_instance

            result = await module.run(request, services)

        mock_cls.assert_called_once()
        assert result.module == "changelog"

    async def test_addition_facts_have_business_rule_category(self) -> None:
        html = """
        <html><body>
          <h2>v1.0.0 – 2024-01-01</h2>
          <ul><li>Added new board feature</li></ul>
        </body></html>
        """
        result = await self._run(html)
        addition_facts = [
            f for f in result.facts
            if f.structured_data.get("change_type") == "addition"
        ]
        assert all(f.category == FactCategory.BUSINESS_RULE for f in addition_facts)

    async def test_deprecation_facts_extracted(self) -> None:
        html = """
        <html><body>
          <h2>v3.0.0 – 2024-02-01</h2>
          <ul><li>Deprecated the legacy export API</li></ul>
        </body></html>
        """
        result = await self._run(html)
        dep_facts = [
            f for f in result.facts
            if f.structured_data.get("change_type") == "deprecation"
        ]
        assert len(dep_facts) >= 1
