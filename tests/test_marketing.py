"""
Tests for scripts/recon/marketing.py — MarketingModule.

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
from scripts.recon.marketing import (
    MarketingModule,
    _MarketingFeature,
    _PricingTier,
)

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
    return httpx.Response(
        status_code=status_code,
        content=html.encode(),
        headers={"content-type": "text/html; charset=utf-8"},
        request=httpx.Request("GET", "https://example.com/pricing"),
    )


_PRICING_HTML = """
<html>
<head><title>Pricing — ExampleApp</title></head>
<body>
  <nav><a href="/">Home</a></nav>
  <section class="pricing-card" id="free-plan">
    <h2>Free</h2>
    <p>Get started at no cost</p>
    <div class="price">$0 / month</div>
    <ul>
      <li>Up to 10 boards</li>
      <li>Basic automation</li>
      <li>2 GB storage</li>
    </ul>
  </section>
  <section class="pricing-card" id="pro-plan">
    <h2>Pro</h2>
    <p>For professionals who need more</p>
    <div class="price">$10 / month</div>
    <ul>
      <li>Unlimited boards</li>
      <li>Advanced automation</li>
      <li>20 GB storage</li>
      <li>Priority support</li>
    </ul>
  </section>
  <section class="pricing-card" id="enterprise-plan">
    <h2>Enterprise</h2>
    <p>Contact us for a custom quote</p>
    <ul>
      <li>Everything in Pro</li>
      <li>SSO / SAML</li>
      <li>Dedicated account manager</li>
    </ul>
  </section>
  <footer>Copyright 2024</footer>
</body>
</html>
"""

_FEATURES_HTML = """
<html>
<head><title>Features — ExampleApp</title></head>
<body>
  <main>
    <h2>Kanban Boards</h2>
    <p>Visualise your workflow with customizable kanban-style boards.</p>

    <h2>Drag and Drop</h2>
    <p>Reorder cards and lists instantly with drag and drop support.</p>

    <h2>Enterprise SSO</h2>
    <p>Requires enterprise plan: Single Sign-On with SAML 2.0 support.</p>

    <ul class="feature-list">
      <li>Real-time collaboration for your whole team</li>
      <li>Automated workflow triggers and actions</li>
      <li>Power-Up integrations with 200+ apps</li>
    </ul>
  </main>
</body>
</html>
"""

_TABLE_PRICING_HTML = """
<html>
<head><title>Compare Plans</title></head>
<body>
  <table>
    <thead>
      <tr>
        <th>Feature</th>
        <th>Free</th>
        <th>Pro</th>
        <th>Enterprise</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td>Price</td>
        <td>$0</td>
        <td>$10/month</td>
        <td>Custom</td>
      </tr>
      <tr>
        <td>Boards</td>
        <td>10</td>
        <td>✓</td>
        <td>✓</td>
      </tr>
      <tr>
        <td>SSO</td>
        <td>✗</td>
        <td>✗</td>
        <td>✓</td>
      </tr>
    </tbody>
  </table>
</body>
</html>
"""

_EMPTY_HTML = "<html><body><p>Nothing here</p></body></html>"


# ---------------------------------------------------------------------------
# Module properties
# ---------------------------------------------------------------------------


class TestModuleProperties:
    def test_name(self) -> None:
        assert MarketingModule().name == "marketing"

    def test_authority(self) -> None:
        assert MarketingModule().authority == Authority.ANECDOTAL

    def test_source_type(self) -> None:
        assert MarketingModule().source_type == SourceType.MARKETING

    def test_requires_credentials_empty(self) -> None:
        assert MarketingModule().requires_credentials == []


# ---------------------------------------------------------------------------
# URL discovery
# ---------------------------------------------------------------------------


class TestDiscoverUrls:
    def test_explicit_urls_take_priority(self) -> None:
        module = MarketingModule()
        urls = module._discover_urls(
            "https://example.com",
            {"marketing_urls": ["https://example.com/pricing", "https://example.com/features"]},
        )
        assert urls == ["https://example.com/pricing", "https://example.com/features"]

    def test_probes_well_known_paths(self) -> None:
        module = MarketingModule()
        urls = module._discover_urls("https://example.com", {})
        assert any("/pricing" in u for u in urls)
        assert any("/features" in u for u in urls)

    def test_base_url_used(self) -> None:
        module = MarketingModule()
        urls = module._discover_urls("https://app.example.com", {})
        assert all(u.startswith("https://app.example.com") for u in urls)


# ---------------------------------------------------------------------------
# Pricing tier parsing
# ---------------------------------------------------------------------------


class TestParsePricingTiers:
    def _parse(
        self, html: str, url: str = "https://example.com/pricing"
    ) -> list[_PricingTier]:
        module = MarketingModule()
        soup = BeautifulSoup(html, "html.parser")
        return module._parse_pricing_tiers(soup, url)

    def test_extracts_free_tier(self) -> None:
        tiers = self._parse(_PRICING_HTML)
        names = [t.name.lower() for t in tiers]
        assert any("free" in n for n in names)

    def test_extracts_pro_tier(self) -> None:
        tiers = self._parse(_PRICING_HTML)
        names = [t.name.lower() for t in tiers]
        assert any("pro" in n for n in names)

    def test_extracts_enterprise_tier(self) -> None:
        tiers = self._parse(_PRICING_HTML)
        names = [t.name.lower() for t in tiers]
        assert any("enterprise" in n for n in names)

    def test_pro_price_extracted(self) -> None:
        tiers = self._parse(_PRICING_HTML)
        pro = next((t for t in tiers if "pro" in t.name.lower()), None)
        assert pro is not None
        assert pro.price_usd == 10.0

    def test_free_tier_price_is_zero_or_free_string(self) -> None:
        tiers = self._parse(_PRICING_HTML)
        free = next((t for t in tiers if "free" in t.name.lower()), None)
        assert free is not None
        assert free.price_usd == 0.0 or free.price_str.lower() in ("free", "$0 / month", "$0")

    def test_enterprise_billing_period_custom(self) -> None:
        tiers = self._parse(_PRICING_HTML)
        ent = next((t for t in tiers if "enterprise" in t.name.lower()), None)
        assert ent is not None
        assert ent.billing_period in ("custom", "monthly")

    def test_source_url_preserved(self) -> None:
        url = "https://example.com/pricing"
        tiers = self._parse(_PRICING_HTML, url)
        for tier in tiers:
            assert tier.source_url == url

    def test_features_extracted_for_tier(self) -> None:
        tiers = self._parse(_PRICING_HTML)
        pro = next((t for t in tiers if "pro" in t.name.lower()), None)
        assert pro is not None
        assert len(pro.features) > 0

    def test_no_duplicate_tier_names(self) -> None:
        tiers = self._parse(_PRICING_HTML)
        names = [t.name.lower() for t in tiers]
        assert len(names) == len(set(names))

    def test_table_pricing_extracts_tiers(self) -> None:
        tiers = self._parse(_TABLE_PRICING_HTML)
        names = [t.name.lower() for t in tiers]
        assert any("free" in n or "pro" in n or "enterprise" in n for n in names)

    def test_empty_page_returns_no_tiers(self) -> None:
        tiers = self._parse(_EMPTY_HTML)
        assert tiers == []


# ---------------------------------------------------------------------------
# Marketing feature parsing
# ---------------------------------------------------------------------------


class TestParseMarketingFeatures:
    def _parse(
        self, html: str, url: str = "https://example.com/features"
    ) -> list[_MarketingFeature]:
        module = MarketingModule()
        soup = BeautifulSoup(html, "html.parser")
        return module._parse_marketing_features(soup, url)

    def test_extracts_heading_features(self) -> None:
        features = self._parse(_FEATURES_HTML)
        names = [f.name for f in features]
        assert any("Kanban" in n or "kanban" in n.lower() for n in names)

    def test_extracts_drag_drop_feature(self) -> None:
        features = self._parse(_FEATURES_HTML)
        names = [f.name for f in features]
        assert any("Drag" in n or "drag" in n.lower() for n in names)

    def test_premium_feature_flagged(self) -> None:
        features = self._parse(_FEATURES_HTML)
        sso = next((f for f in features if "SSO" in f.name or "sso" in f.name.lower()), None)
        assert sso is not None
        assert sso.is_premium is True

    def test_free_feature_not_flagged_as_premium(self) -> None:
        features = self._parse(_FEATURES_HTML)
        kanban = next(
            (f for f in features if "Kanban" in f.name or "kanban" in f.name.lower()), None
        )
        assert kanban is not None
        assert kanban.is_premium is False

    def test_list_items_extracted(self) -> None:
        features = self._parse(_FEATURES_HTML)
        # "Real-time collaboration" is in a <ul> list
        names_and_descs = [(f.name, f.description) for f in features]
        found = any("collaboration" in (n + d).lower() for n, d in names_and_descs)
        assert found

    def test_source_url_preserved(self) -> None:
        url = "https://example.com/features"
        features = self._parse(_FEATURES_HTML, url)
        for f in features:
            assert f.source_url == url

    def test_empty_page_returns_no_features(self) -> None:
        features = self._parse(_EMPTY_HTML)
        assert features == []

    def test_no_duplicate_feature_names(self) -> None:
        features = self._parse(_FEATURES_HTML)
        keys = [f.name.lower()[:60] for f in features]
        assert len(keys) == len(set(keys))


# ---------------------------------------------------------------------------
# Fact creation — pricing tier
# ---------------------------------------------------------------------------


class TestTierToFact:
    def setup_method(self) -> None:
        self.module = MarketingModule()
        self.run_id = str(uuid.uuid4())

    def _make_tier(self, **kwargs: Any) -> _PricingTier:
        defaults: dict[str, Any] = {
            "name": "Pro",
            "price_str": "$10/month",
            "price_usd": 10.0,
            "billing_period": "monthly",
            "features": ["Unlimited boards", "Advanced automation"],
            "source_url": "https://example.com/pricing",
            "raw_excerpt": "Pro $10/month: Unlimited boards",
        }
        defaults.update(kwargs)
        return _PricingTier(**defaults)

    def test_category_is_business_rule(self) -> None:
        tier = self._make_tier()
        fact = self.module._tier_to_fact(tier, self.run_id, [])
        assert fact.category == FactCategory.BUSINESS_RULE

    def test_authority_anecdotal(self) -> None:
        tier = self._make_tier()
        fact = self.module._tier_to_fact(tier, self.run_id, [])
        assert fact.authority == Authority.ANECDOTAL

    def test_source_type_marketing(self) -> None:
        tier = self._make_tier()
        fact = self.module._tier_to_fact(tier, self.run_id, [])
        assert fact.source_type == SourceType.MARKETING

    def test_module_name_set(self) -> None:
        tier = self._make_tier()
        fact = self.module._tier_to_fact(tier, self.run_id, [])
        assert fact.module_name == "marketing"

    def test_confidence_low(self) -> None:
        tier = self._make_tier()
        fact = self.module._tier_to_fact(tier, self.run_id, [])
        assert fact.confidence == Confidence.LOW

    def test_evidence_has_source_url(self) -> None:
        tier = self._make_tier(source_url="https://example.com/pricing")
        fact = self.module._tier_to_fact(tier, self.run_id, [])
        assert len(fact.evidence) == 1
        assert fact.evidence[0].source_url == "https://example.com/pricing"

    def test_run_id_set(self) -> None:
        tier = self._make_tier()
        fact = self.module._tier_to_fact(tier, self.run_id, [])
        assert fact.run_id == self.run_id

    def test_claim_contains_tier_name(self) -> None:
        tier = self._make_tier(name="Pro")
        fact = self.module._tier_to_fact(tier, self.run_id, [])
        assert "Pro" in fact.claim

    def test_structured_data_has_tier_name(self) -> None:
        tier = self._make_tier(name="Enterprise")
        fact = self.module._tier_to_fact(tier, self.run_id, [])
        assert fact.structured_data["tier_name"] == "Enterprise"

    def test_structured_data_has_price(self) -> None:
        tier = self._make_tier(price_usd=10.0)
        fact = self.module._tier_to_fact(tier, self.run_id, [])
        assert fact.structured_data["price_usd"] == 10.0

    def test_structured_data_has_features(self) -> None:
        tier = self._make_tier(features=["Unlimited boards"])
        fact = self.module._tier_to_fact(tier, self.run_id, [])
        assert "Unlimited boards" in fact.structured_data["features"]


# ---------------------------------------------------------------------------
# Fact creation — marketing feature
# ---------------------------------------------------------------------------


class TestFeatureToFact:
    def setup_method(self) -> None:
        self.module = MarketingModule()
        self.run_id = str(uuid.uuid4())

    def _make_feature(self, **kwargs: Any) -> _MarketingFeature:
        defaults: dict[str, Any] = {
            "name": "Kanban Boards",
            "description": "Visualise your workflow with customizable boards.",
            "tiers": ["Free", "Pro"],
            "is_premium": False,
            "source_url": "https://example.com/features",
            "raw_excerpt": "Kanban Boards: Visualise your workflow.",
            "tags": ["boards"],
        }
        defaults.update(kwargs)
        return _MarketingFeature(**defaults)

    def test_category_is_configuration(self) -> None:
        feature = self._make_feature()
        fact = self.module._feature_to_fact(feature, self.run_id, [])
        assert fact.category == FactCategory.CONFIGURATION

    def test_authority_anecdotal(self) -> None:
        feature = self._make_feature()
        fact = self.module._feature_to_fact(feature, self.run_id, [])
        assert fact.authority == Authority.ANECDOTAL

    def test_source_type_marketing(self) -> None:
        feature = self._make_feature()
        fact = self.module._feature_to_fact(feature, self.run_id, [])
        assert fact.source_type == SourceType.MARKETING

    def test_module_name_set(self) -> None:
        feature = self._make_feature()
        fact = self.module._feature_to_fact(feature, self.run_id, [])
        assert fact.module_name == "marketing"

    def test_confidence_low(self) -> None:
        feature = self._make_feature()
        fact = self.module._feature_to_fact(feature, self.run_id, [])
        assert fact.confidence == Confidence.LOW

    def test_evidence_has_source_url(self) -> None:
        feature = self._make_feature(source_url="https://example.com/features")
        fact = self.module._feature_to_fact(feature, self.run_id, [])
        assert len(fact.evidence) == 1
        assert fact.evidence[0].source_url == "https://example.com/features"

    def test_run_id_set(self) -> None:
        feature = self._make_feature()
        fact = self.module._feature_to_fact(feature, self.run_id, [])
        assert fact.run_id == self.run_id

    def test_claim_contains_feature_name(self) -> None:
        feature = self._make_feature(name="Kanban Boards")
        fact = self.module._feature_to_fact(feature, self.run_id, [])
        assert "Kanban Boards" in fact.claim

    def test_claim_mentions_premium_when_flagged(self) -> None:
        feature = self._make_feature(is_premium=True)
        fact = self.module._feature_to_fact(feature, self.run_id, [])
        assert "paid" in fact.claim.lower() or "premium" in fact.claim.lower()

    def test_structured_data_has_is_premium(self) -> None:
        feature = self._make_feature(is_premium=True)
        fact = self.module._feature_to_fact(feature, self.run_id, [])
        assert fact.structured_data["is_premium"] is True

    def test_structured_data_has_feature_name(self) -> None:
        feature = self._make_feature(name="Drag and Drop")
        fact = self.module._feature_to_fact(feature, self.run_id, [])
        assert fact.structured_data["feature_name"] == "Drag and Drop"

    def test_scope_feature_matched(self) -> None:
        feature = self._make_feature(
            name="Board Management", description="Create and manage boards."
        )
        fact = self.module._feature_to_fact(feature, self.run_id, ["boards", "cards"])
        assert fact.feature in ("boards", "board", "management") or fact.feature == "boards"


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
        module = MarketingModule()
        request = _make_request(
            module_config=module_config
            or {"marketing_urls": ["https://example.com/pricing"]},
        )
        mock_response = _make_http_response(html, status_code)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        services = _make_services(http_client=mock_client)
        return await module.run(request, services)

    async def test_success_status_when_facts_found(self) -> None:
        result = await self._run(_PRICING_HTML)
        assert result.status == ReconModuleStatus.SUCCESS
        assert result.module == "marketing"

    async def test_facts_produced(self) -> None:
        result = await self._run(_PRICING_HTML)
        assert len(result.facts) > 0

    async def test_all_facts_have_correct_module(self) -> None:
        result = await self._run(_PRICING_HTML)
        for fact in result.facts:
            assert fact.module_name == "marketing"

    async def test_all_facts_have_evidence(self) -> None:
        result = await self._run(_PRICING_HTML)
        for fact in result.facts:
            assert len(fact.evidence) >= 1

    async def test_all_facts_have_authority_anecdotal(self) -> None:
        result = await self._run(_PRICING_HTML)
        for fact in result.facts:
            assert fact.authority == Authority.ANECDOTAL

    async def test_all_facts_have_source_type_marketing(self) -> None:
        result = await self._run(_PRICING_HTML)
        for fact in result.facts:
            assert fact.source_type == SourceType.MARKETING

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
        module = MarketingModule()
        request = _make_request(
            module_config={"marketing_urls": ["https://example.com/pricing"]},
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=httpx.RequestError("connection refused"))
        mock_client.aclose = AsyncMock()
        services = _make_services(http_client=mock_client)

        result = await module.run(request, services)
        assert result.module == "marketing"
        assert result.status == ReconModuleStatus.FAILED

    async def test_run_does_not_raise_on_unexpected_exception(self) -> None:
        """INV-020: even unexpected errors are caught."""
        module = MarketingModule()
        request = _make_request(
            module_config={"marketing_urls": ["https://example.com/pricing"]},
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(side_effect=RuntimeError("completely unexpected"))
        mock_client.aclose = AsyncMock()
        services = _make_services(http_client=mock_client)

        result = await module.run(request, services)
        assert result.module == "marketing"
        assert result.status == ReconModuleStatus.FAILED

    async def test_result_module_matches_name(self) -> None:
        result = await self._run(_PRICING_HTML)
        assert result.module == "marketing"

    async def test_progress_callback_called(self) -> None:
        module = MarketingModule()
        request = _make_request(
            module_config={"marketing_urls": ["https://example.com/pricing"]},
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = AsyncMock(return_value=_make_http_response(_PRICING_HTML))
        mock_client.aclose = AsyncMock()
        services = _make_services(http_client=mock_client)

        progress_calls: list[Any] = []
        await module.run(request, services, progress=progress_calls.append)
        assert len(progress_calls) > 0

    async def test_metrics_populated(self) -> None:
        result = await self._run(_PRICING_HTML)
        assert "facts_found" in result.metrics
        assert "errors" in result.metrics
        assert "urls_visited" in result.metrics

    async def test_urls_visited_populated(self) -> None:
        result = await self._run(_PRICING_HTML)
        assert len(result.urls_visited) > 0

    async def test_creates_own_client_when_none_injected(self) -> None:
        """When services.http_client is None, module creates its own client."""
        module = MarketingModule()
        request = _make_request(
            module_config={"marketing_urls": ["https://example.com/pricing"]},
        )
        services = _make_services(http_client=None)

        with patch("scripts.recon.marketing.httpx.AsyncClient") as mock_cls:
            mock_instance = AsyncMock()
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_instance.get = AsyncMock(return_value=_make_http_response(_PRICING_HTML))
            mock_instance.aclose = AsyncMock()
            mock_cls.return_value = mock_instance

            result = await module.run(request, services)

        mock_cls.assert_called_once()
        assert result.module == "marketing"

    async def test_pricing_facts_have_business_rule_category(self) -> None:
        result = await self._run(_PRICING_HTML)
        pricing_facts = [f for f in result.facts if f.category == FactCategory.BUSINESS_RULE]
        assert len(pricing_facts) >= 1

    async def test_feature_facts_have_configuration_category(self) -> None:
        result = await self._run(_FEATURES_HTML)
        feature_facts = [f for f in result.facts if f.category == FactCategory.CONFIGURATION]
        assert len(feature_facts) >= 1

    async def test_rate_limit_captured_as_error(self) -> None:
        """429 responses produce rate_limited errors in the result."""
        module = MarketingModule()

        async def mock_get(url: str, **kwargs: Any) -> httpx.Response:
            return httpx.Response(
                status_code=429,
                content=b"Too Many Requests",
                headers={"content-type": "text/html", "Retry-After": "0"},
                request=httpx.Request("GET", url),
            )

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = mock_get
        mock_client.aclose = AsyncMock()

        request = _make_request(
            module_config={"marketing_urls": ["https://example.com/pricing"]},
        )
        services = _make_services(http_client=mock_client)

        result = await module.run(request, services)
        assert isinstance(result, ReconResult)
        assert result.status in (ReconModuleStatus.FAILED, ReconModuleStatus.PARTIAL)

    async def test_partial_status_when_some_errors(self) -> None:
        """PARTIAL status when some URLs succeed and others fail."""
        module = MarketingModule()
        request = _make_request(
            module_config={
                "marketing_urls": [
                    "https://example.com/pricing",
                    "https://example.com/features",
                ]
            },
        )

        call_count = 0

        async def side_effect(url: str, **kwargs: Any) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_http_response(_PRICING_HTML)
            return httpx.Response(
                status_code=503,
                content=b"Service Unavailable",
                headers={"content-type": "text/html"},
                request=httpx.Request("GET", url),
            )

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.get = side_effect
        mock_client.aclose = AsyncMock()
        services = _make_services(http_client=mock_client)

        result = await module.run(request, services)
        assert result.status in (ReconModuleStatus.SUCCESS, ReconModuleStatus.PARTIAL)
        assert len(result.facts) > 0
