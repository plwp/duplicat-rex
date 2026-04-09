"""
Tests for source-level noise filtering in browser_explore and hypothesis_builder.

Covers:
  - _is_product_request: static assets, analytics domains, same-origin API kept
  - CapturedRequest.content_type field
  - response_body stored and response_fields extracted in structured_data
  - HypothesisBuilder._is_product_entity: infrastructure filtered, product kept
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import MagicMock

import pytest

from scripts.models import (
    Authority,
    EvidenceRef,
    Fact,
    FactCategory,
    SourceType,
)
from scripts.recon.base import (
    ReconRequest,
    ReconServices,
)
from scripts.recon.browser_explore import (
    BrowserExploreModule,
    CapturedRequest,
    NavigationStep,
)
from scripts.hypothesis_builder import HypothesisBuilder, _is_product_entity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_request(**kwargs: Any) -> ReconRequest:
    defaults: dict[str, Any] = {
        "run_id": str(uuid.uuid4()),
        "target": "trello.com",
        "base_url": "https://trello.com",
        "budgets": {"max_pages": 3, "time_seconds": 30},
    }
    defaults.update(kwargs)
    return ReconRequest(**defaults)


def make_services(**kwargs: Any) -> ReconServices:
    defaults: dict[str, Any] = {
        "spec_store": MagicMock(),
        "credentials": {},
        "artifact_store": None,
        "http_client": MagicMock(),
        "browser": None,
        "clock": None,
    }
    defaults.update(kwargs)
    return ReconServices(**defaults)


def make_api_fact(path: str, method: str = "GET", feature: str = "boards") -> Fact:
    return Fact(
        feature=feature,
        category=FactCategory.API_ENDPOINT,
        claim=f"{method} {path}",
        evidence=[EvidenceRef(source_url=f"https://trello.com{path}")],
        source_type=SourceType.LIVE_APP,
        authority=Authority.AUTHORITATIVE,
        structured_data={"method": method, "url": f"https://trello.com{path}"},
    )


# ---------------------------------------------------------------------------
# _is_product_request: static asset filtering
# ---------------------------------------------------------------------------


class TestIsProductRequestStaticAssets:
    def test_js_file_filtered(self) -> None:
        assert BrowserExploreModule._is_product_request(
            "https://trello.com/app.bundle.js", "trello.com"
        ) is False

    def test_css_file_filtered(self) -> None:
        assert BrowserExploreModule._is_product_request(
            "https://trello.com/styles.css", "trello.com"
        ) is False

    def test_png_file_filtered(self) -> None:
        assert BrowserExploreModule._is_product_request(
            "https://trello.com/assets/logo.png", "trello.com"
        ) is False

    def test_woff2_font_filtered(self) -> None:
        assert BrowserExploreModule._is_product_request(
            "https://trello.com/fonts/inter.woff2", "trello.com"
        ) is False

    def test_sourcemap_filtered(self) -> None:
        assert BrowserExploreModule._is_product_request(
            "https://trello.com/main.js.map", "trello.com"
        ) is False

    def test_webpack_chunk_hash_filtered(self) -> None:
        # Pattern: /abc1f2e3.deadbeef.js
        assert BrowserExploreModule._is_product_request(
            "https://trello.com/abc1f2e3.deadbeef.chunk.js", "trello.com"
        ) is False

    def test_ico_filtered(self) -> None:
        assert BrowserExploreModule._is_product_request(
            "https://trello.com/favicon.ico", "trello.com"
        ) is False

    def test_webp_filtered(self) -> None:
        assert BrowserExploreModule._is_product_request(
            "https://trello.com/hero.webp", "trello.com"
        ) is False


# ---------------------------------------------------------------------------
# _is_product_request: analytics domain filtering
# ---------------------------------------------------------------------------


class TestIsProductRequestAnalyticsDomains:
    def test_google_analytics_filtered(self) -> None:
        assert BrowserExploreModule._is_product_request(
            "https://www.google-analytics.com/collect", "trello.com"
        ) is False

    def test_googleads_doubleclick_filtered(self) -> None:
        assert BrowserExploreModule._is_product_request(
            "https://googleads.g.doubleclick.net/pagead/viewthroughconversion/123",
            "trello.com",
        ) is False

    def test_sentry_io_filtered(self) -> None:
        assert BrowserExploreModule._is_product_request(
            "https://sentry.io/api/1/envelope/", "trello.com"
        ) is False

    def test_facebook_connect_filtered(self) -> None:
        assert BrowserExploreModule._is_product_request(
            "https://connect.facebook.net/en_US/fbevents.js", "trello.com"
        ) is False

    def test_recaptcha_filtered(self) -> None:
        assert BrowserExploreModule._is_product_request(
            "https://www.recaptcha.net/recaptcha/api.js", "trello.com"
        ) is False

    def test_bat_bing_filtered(self) -> None:
        assert BrowserExploreModule._is_product_request(
            "https://bat.bing.com/action/0?ti=123", "trello.com"
        ) is False


# ---------------------------------------------------------------------------
# _is_product_request: noise path segments
# ---------------------------------------------------------------------------


class TestIsProductRequestNoisePaths:
    def test_gasv3_path_filtered(self) -> None:
        assert BrowserExploreModule._is_product_request(
            "https://trello.com/1/gasv3/api/token", "trello.com"
        ) is False

    def test_analytics_path_filtered(self) -> None:
        assert BrowserExploreModule._is_product_request(
            "https://example.com/analytics/track", "trello.com"
        ) is False

    def test_flagcdn_path_filtered(self) -> None:
        assert BrowserExploreModule._is_product_request(
            "https://cdn.flagcdn.com/w20/us.png", "trello.com"
        ) is False

    def test_contentful_path_filtered(self) -> None:
        assert BrowserExploreModule._is_product_request(
            "https://cdn.contentful.com/spaces/abc/entries", "trello.com"
        ) is False

    def test_onetrust_path_filtered(self) -> None:
        assert BrowserExploreModule._is_product_request(
            "https://cdn.cookielaw.org/onetrust/consent.js", "trello.com"
        ) is False


# ---------------------------------------------------------------------------
# _is_product_request: same-origin API calls kept
# ---------------------------------------------------------------------------


class TestIsProductRequestSameOriginKept:
    def test_trello_api_v1_boards_kept(self) -> None:
        assert BrowserExploreModule._is_product_request(
            "https://trello.com/1/boards", "trello.com"
        ) is True

    def test_trello_api_v1_cards_kept(self) -> None:
        assert BrowserExploreModule._is_product_request(
            "https://trello.com/1/cards/abc123", "trello.com"
        ) is True

    def test_api_path_kept(self) -> None:
        assert BrowserExploreModule._is_product_request(
            "https://example.com/api/v2/users", "example.com"
        ) is True

    def test_json_endpoint_kept(self) -> None:
        assert BrowserExploreModule._is_product_request(
            "https://trello.com/app/rest/v2/projects", "trello.com"
        ) is True

    def test_different_origin_not_kept(self) -> None:
        # Third-party origin not in noise lists — returns False (unknown origin)
        assert BrowserExploreModule._is_product_request(
            "https://some-cdn.example.net/data.json", "trello.com"
        ) is False


# ---------------------------------------------------------------------------
# CapturedRequest: content_type field exists
# ---------------------------------------------------------------------------


class TestCapturedRequestContentType:
    def test_content_type_field_defaults_empty(self) -> None:
        req = CapturedRequest(url="https://trello.com/1/boards", method="GET")
        assert req.content_type == ""

    def test_content_type_field_can_be_set(self) -> None:
        req = CapturedRequest(
            url="https://trello.com/1/boards",
            method="GET",
            content_type="application/json; charset=utf-8",
        )
        assert req.content_type == "application/json; charset=utf-8"


# ---------------------------------------------------------------------------
# response_body captured and response_fields in structured_data
# ---------------------------------------------------------------------------


class TestResponseBodyCapture:
    """Test that response_body is stored and JSON fields are extracted into structured_data."""

    def _run_extract(self, steps: list[NavigationStep]) -> tuple:
        module = BrowserExploreModule()
        request = make_request()
        services = make_services()
        return module._extract_facts(steps, request, services)

    def test_response_fields_extracted_from_json_body(self) -> None:
        body = json.dumps({"id": "abc", "name": "My Board", "closed": False})
        req = CapturedRequest(
            url="https://trello.com/1/boards/abc",
            method="GET",
            response_status=200,
            response_headers={"content-type": "application/json"},
            response_body=body,
            content_type="application/json",
        )
        step = NavigationStep(
            url="https://trello.com/boards",
            page_title="Boards",
            http_requests=[req],
            ws_frames=[],
        )
        facts, _, _, errors = self._run_extract([step])
        assert not errors
        api_facts = [f for f in facts if f.category == FactCategory.API_ENDPOINT]
        assert api_facts
        structured = api_facts[0].structured_data
        assert "response_fields" in structured
        assert "id" in structured["response_fields"]
        assert "name" in structured["response_fields"]
        assert "closed" in structured["response_fields"]

    def test_response_sample_type_names(self) -> None:
        body = json.dumps({"id": "abc", "count": 42, "active": True})
        req = CapturedRequest(
            url="https://trello.com/1/boards",
            method="GET",
            response_status=200,
            response_body=body,
            content_type="application/json",
        )
        step = NavigationStep(
            url="https://trello.com/boards",
            page_title="Boards",
            http_requests=[req],
            ws_frames=[],
        )
        facts, _, _, _ = self._run_extract([step])
        api_facts = [f for f in facts if f.category == FactCategory.API_ENDPOINT]
        assert api_facts
        sample = api_facts[0].structured_data.get("response_sample", {})
        assert sample.get("id") == "str"
        assert sample.get("count") == "int"
        assert sample.get("active") == "bool"

    def test_non_json_body_no_response_fields(self) -> None:
        req = CapturedRequest(
            url="https://trello.com/1/boards",
            method="GET",
            response_status=200,
            response_body="not json at all",
            content_type="text/plain",
        )
        step = NavigationStep(
            url="https://trello.com/boards",
            page_title="Boards",
            http_requests=[req],
            ws_frames=[],
        )
        facts, _, _, _ = self._run_extract([step])
        api_facts = [f for f in facts if f.category == FactCategory.API_ENDPOINT]
        assert api_facts
        structured = api_facts[0].structured_data
        assert "response_fields" not in structured

    def test_json_array_body_no_response_fields(self) -> None:
        body = json.dumps([{"id": "a"}, {"id": "b"}])
        req = CapturedRequest(
            url="https://trello.com/1/boards",
            method="GET",
            response_status=200,
            response_body=body,
        )
        step = NavigationStep(
            url="https://trello.com/boards",
            page_title="Boards",
            http_requests=[req],
            ws_frames=[],
        )
        facts, _, _, _ = self._run_extract([step])
        api_facts = [f for f in facts if f.category == FactCategory.API_ENDPOINT]
        assert api_facts
        assert "response_fields" not in api_facts[0].structured_data

    def test_response_body_sample_included_if_small(self) -> None:
        body = json.dumps({"id": "abc"})
        req = CapturedRequest(
            url="https://trello.com/1/boards",
            method="GET",
            response_status=200,
            response_body=body,
        )
        step = NavigationStep(
            url="https://trello.com/boards",
            page_title="Boards",
            http_requests=[req],
            ws_frames=[],
        )
        facts, _, _, _ = self._run_extract([step])
        api_facts = [f for f in facts if f.category == FactCategory.API_ENDPOINT]
        assert api_facts
        assert "response_body_sample" in api_facts[0].structured_data


# ---------------------------------------------------------------------------
# HypothesisBuilder: infrastructure entities filtered
# ---------------------------------------------------------------------------


class TestHypothesisBuilderFiltersInfrastructure:
    def test_gateway_not_in_model(self) -> None:
        facts = [make_api_fact("/gateway/api/graphql")]
        model = HypothesisBuilder().build(facts, "trello.com")
        assert "Gateway" not in model.entities

    def test_consent_not_in_model(self) -> None:
        facts = [make_api_fact("/consent/v1/user-consent")]
        model = HypothesisBuilder().build(facts, "trello.com")
        assert "Consent" not in model.entities

    def test_session_not_in_model(self) -> None:
        facts = [make_api_fact("/session/refresh")]
        model = HypothesisBuilder().build(facts, "trello.com")
        assert "Session" not in model.entities

    def test_heartbeat_not_in_model(self) -> None:
        facts = [make_api_fact("/heartbeat")]
        model = HypothesisBuilder().build(facts, "trello.com")
        assert "Heartbeat" not in model.entities

    def test_single_letter_path_not_in_model(self) -> None:
        facts = [make_api_fact("/px/something")]
        model = HypothesisBuilder().build(facts, "trello.com")
        assert "Px" not in model.entities

    def test_two_letter_path_not_in_model(self) -> None:
        facts = [make_api_fact("/wa/event")]
        model = HypothesisBuilder().build(facts, "trello.com")
        assert "Wa" not in model.entities

    def test_hex_uuid_not_in_model(self) -> None:
        # A path segment that is a hex UUID should not become an entity
        assert _is_product_entity("deadbeef-1234-5678") is False

    def test_path_with_colon_not_entity(self) -> None:
        assert _is_product_entity("some:thing") is False

    def test_path_with_dot_not_entity(self) -> None:
        assert _is_product_entity("file.json") is False


# ---------------------------------------------------------------------------
# HypothesisBuilder: product entities kept
# ---------------------------------------------------------------------------


class TestHypothesisBuilderKeepsProductEntities:
    def test_board_kept(self) -> None:
        facts = [make_api_fact("/1/boards")]
        model = HypothesisBuilder().build(facts, "trello.com")
        assert "Board" in model.entities

    def test_card_kept(self) -> None:
        facts = [make_api_fact("/1/cards")]
        model = HypothesisBuilder().build(facts, "trello.com")
        assert "Card" in model.entities

    def test_list_kept(self) -> None:
        facts = [make_api_fact("/1/lists")]
        model = HypothesisBuilder().build(facts, "trello.com")
        assert "List" in model.entities

    def test_member_kept(self) -> None:
        facts = [make_api_fact("/1/members")]
        model = HypothesisBuilder().build(facts, "trello.com")
        assert "Member" in model.entities

    def test_organization_kept(self) -> None:
        facts = [make_api_fact("/1/organizations")]
        model = HypothesisBuilder().build(facts, "trello.com")
        assert "Organization" in model.entities

    def test_product_entity_function_returns_true_for_board(self) -> None:
        assert _is_product_entity("boards") is True

    def test_product_entity_function_returns_true_for_cards(self) -> None:
        assert _is_product_entity("cards") is True

    def test_infrastructure_entity_function_returns_false_for_gateway(self) -> None:
        assert _is_product_entity("gateway") is False

    def test_infrastructure_entity_function_returns_false_for_resolve(self) -> None:
        assert _is_product_entity("resolve") is False
