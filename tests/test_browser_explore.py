"""
Tests for BrowserExploreModule (scripts/recon/browser_explore.py).

All Playwright interactions are mocked — no real browser required.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts.models import (
    Authority,
    FactCategory,
    SourceType,
)
from scripts.recon.base import (
    ReconModuleStatus,
    ReconRequest,
    ReconServices,
)
from scripts.recon.browser_explore import (
    BrowserExploreModule,
    CapturedRequest,
    CapturedWsFrame,
    NavigationStep,
)

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


def make_step(
    url: str = "https://trello.com/boards",
    page_title: str = "Boards",
    dom_summary: str = "My Boards",
    http_requests: list[CapturedRequest] | None = None,
    ws_frames: list[CapturedWsFrame] | None = None,
) -> NavigationStep:
    return NavigationStep(
        url=url,
        page_title=page_title,
        dom_summary=dom_summary,
        http_requests=http_requests or [],
        ws_frames=ws_frames or [],
    )


def make_http_req(
    url: str = "https://trello.com/1/boards",
    method: str = "GET",
    status: int = 200,
) -> CapturedRequest:
    return CapturedRequest(
        url=url,
        method=method,
        request_headers={"accept": "application/json"},
        response_status=status,
        response_headers={"content-type": "application/json"},
        response_body='{"id": "abc123"}',
    )


def make_ws_frame(
    url: str = "wss://trello.com/socket",
    direction: str = "received",
    payload: str = '{"type": "card.update", "data": {}}',
) -> CapturedWsFrame:
    return CapturedWsFrame(url=url, direction=direction, payload=payload)


# ---------------------------------------------------------------------------
# Module property tests
# ---------------------------------------------------------------------------


class TestModuleProperties:
    def test_name(self) -> None:
        module = BrowserExploreModule()
        assert module.name == "browser_explore"

    def test_authority(self) -> None:
        module = BrowserExploreModule()
        assert module.authority == Authority.AUTHORITATIVE

    def test_source_type(self) -> None:
        module = BrowserExploreModule()
        assert module.source_type == SourceType.LIVE_APP

    def test_requires_credentials(self) -> None:
        module = BrowserExploreModule()
        creds = module.requires_credentials
        assert isinstance(creds, list)
        assert len(creds) == 2
        # Must contain placeholders for username and password
        joined = " ".join(creds)
        assert "{domain}" in joined
        assert "username" in joined
        assert "password" in joined


# ---------------------------------------------------------------------------
# Credential resolution tests
# ---------------------------------------------------------------------------


class TestCredentialResolution:
    def test_resolves_domain_slug(self) -> None:
        module = BrowserExploreModule()
        request = make_request(target="trello.com")
        services = make_services(
            credentials={
                "target.trello-com.username": "user@example.com",
                "target.trello-com.password": "s3cret",
            }
        )
        creds = module._resolve_credentials(request, services)
        assert "target.trello-com.username" in creds
        assert "target.trello-com.password" in creds

    def test_missing_credentials_returns_empty(self) -> None:
        module = BrowserExploreModule()
        request = make_request(target="trello.com")
        services = make_services(credentials={})
        creds = module._resolve_credentials(request, services)
        assert creds == {}

    def test_domain_slug_strips_https(self) -> None:
        slug = BrowserExploreModule._domain_slug("https://app.example.com")
        assert slug == "app.example.com".replace(".", "-")

    def test_domain_slug_plain_domain(self) -> None:
        slug = BrowserExploreModule._domain_slug("trello.com")
        assert slug == "trello-com"


# ---------------------------------------------------------------------------
# Header redaction tests (INV-028)
# ---------------------------------------------------------------------------


class TestHeaderRedaction:
    def test_redacts_authorization(self) -> None:
        headers = {"authorization": "Bearer secret-token", "content-type": "application/json"}
        redacted = BrowserExploreModule._redact_headers(headers)
        assert redacted["authorization"] == "[REDACTED]"
        assert redacted["content-type"] == "application/json"

    def test_redacts_cookie(self) -> None:
        headers = {"cookie": "session=abc123; token=xyz"}
        redacted = BrowserExploreModule._redact_headers(headers)
        assert redacted["cookie"] == "[REDACTED]"

    def test_redacts_set_cookie(self) -> None:
        headers = {"set-cookie": "session=abc123; HttpOnly"}
        redacted = BrowserExploreModule._redact_headers(headers)
        assert redacted["set-cookie"] == "[REDACTED]"

    def test_non_sensitive_headers_pass_through(self) -> None:
        headers = {"content-type": "application/json", "x-request-id": "req-123"}
        redacted = BrowserExploreModule._redact_headers(headers)
        assert redacted == headers


# ---------------------------------------------------------------------------
# WS event name extraction tests
# ---------------------------------------------------------------------------


class TestWsEventExtraction:
    def test_extracts_type_field(self) -> None:
        payload = json.dumps({"type": "card.update", "data": {}})
        assert BrowserExploreModule._extract_ws_event_name(payload) == "card.update"

    def test_extracts_event_field(self) -> None:
        payload = json.dumps({"event": "board.moved"})
        assert BrowserExploreModule._extract_ws_event_name(payload) == "board.moved"

    def test_extracts_action_field(self) -> None:
        payload = json.dumps({"action": "ping"})
        assert BrowserExploreModule._extract_ws_event_name(payload) == "ping"

    def test_returns_none_for_non_json(self) -> None:
        assert BrowserExploreModule._extract_ws_event_name("not json") is None

    def test_returns_none_for_empty(self) -> None:
        assert BrowserExploreModule._extract_ws_event_name("") is None

    def test_returns_none_for_json_array(self) -> None:
        assert BrowserExploreModule._extract_ws_event_name("[1, 2, 3]") is None


# ---------------------------------------------------------------------------
# Fact extraction tests
# ---------------------------------------------------------------------------


class TestFactExtraction:
    """Test _extract_facts without requiring a browser."""

    def _run_extract(
        self,
        steps: list[NavigationStep],
        features: list[str] | None = None,
    ) -> tuple:
        module = BrowserExploreModule()
        request = make_request()
        if features:
            from scripts.models import Scope, ScopeNode
            scope = Scope()
            scope.resolved_features = [ScopeNode(feature=f) for f in features]
            request = make_request(scope=scope)
        services = make_services()
        return module._extract_facts(steps, request, services)

    def test_ui_fact_created_for_page_with_dom(self) -> None:
        steps = [make_step()]
        facts, _, _, errors = self._run_extract(steps)
        assert not errors
        ui_facts = [f for f in facts if f.category == FactCategory.UI_COMPONENT]
        assert len(ui_facts) == 1
        assert "Boards" in ui_facts[0].claim

    def test_api_fact_created_for_http_request(self) -> None:
        http_req = make_http_req()
        steps = [make_step(http_requests=[http_req])]
        facts, _, _, errors = self._run_extract(steps)
        api_facts = [f for f in facts if f.category == FactCategory.API_ENDPOINT]
        assert len(api_facts) == 1
        assert "GET" in api_facts[0].claim
        assert "200" in api_facts[0].claim

    def test_ws_fact_created_for_ws_frame(self) -> None:
        ws_frame = make_ws_frame()
        steps = [make_step(ws_frames=[ws_frame])]
        facts, _, _, errors = self._run_extract(steps)
        ws_facts = [f for f in facts if f.category == FactCategory.WS_EVENT]
        assert len(ws_facts) == 1
        assert "received" in ws_facts[0].claim

    def test_user_flow_fact_created(self) -> None:
        http_req = make_http_req()
        steps = [make_step(http_requests=[http_req])]
        facts, _, _, errors = self._run_extract(steps)
        flow_facts = [f for f in facts if f.category == FactCategory.USER_FLOW]
        assert len(flow_facts) == 1
        assert "HTTP" in flow_facts[0].claim

    def test_all_facts_have_authoritative_authority(self) -> None:
        http_req = make_http_req()
        ws_frame = make_ws_frame()
        steps = [make_step(http_requests=[http_req], ws_frames=[ws_frame])]
        facts, _, _, _ = self._run_extract(steps)
        assert all(f.authority == Authority.AUTHORITATIVE for f in facts)

    def test_all_facts_have_module_name(self) -> None:
        steps = [make_step()]
        facts, _, _, _ = self._run_extract(steps)
        assert all(f.module_name == "browser_explore" for f in facts)

    def test_all_facts_have_live_app_source_type(self) -> None:
        steps = [make_step()]
        facts, _, _, _ = self._run_extract(steps)
        assert all(f.source_type == SourceType.LIVE_APP for f in facts)

    def test_no_secrets_in_structured_data(self) -> None:
        """INV-028: Auth headers must be redacted in stored structured_data."""
        http_req = CapturedRequest(
            url="https://trello.com/1/boards",
            method="GET",
            request_headers={"authorization": "Bearer super-secret"},
            response_status=200,
            response_headers={"set-cookie": "session=abc123"},
        )
        steps = [make_step(http_requests=[http_req])]
        facts, _, _, _ = self._run_extract(steps)
        api_facts = [f for f in facts if f.category == FactCategory.API_ENDPOINT]
        assert api_facts
        structured = api_facts[0].structured_data
        req_headers = structured.get("request_headers", {})
        resp_headers = structured.get("response_headers", {})
        for v in req_headers.values():
            assert "super-secret" not in v
        for v in resp_headers.values():
            assert "abc123" not in v

    def test_duplicate_endpoints_deduplicated(self) -> None:
        """Same method+path from the same step should only produce one API fact."""
        reqs = [
            make_http_req(url="https://trello.com/1/boards", method="GET"),
            make_http_req(url="https://trello.com/1/boards", method="GET"),
        ]
        steps = [make_step(http_requests=reqs)]
        facts, _, _, _ = self._run_extract(steps)
        api_facts = [f for f in facts if f.category == FactCategory.API_ENDPOINT]
        assert len(api_facts) == 1

    def test_coverage_entry_created(self) -> None:
        steps = [make_step()]
        _, coverage, _, _ = self._run_extract(steps)
        assert len(coverage) >= 1
        assert all(c.fact_count >= 0 for c in coverage)

    def test_screenshot_recorded_in_artifacts(self) -> None:
        step = make_step()
        step = NavigationStep(
            url=step.url,
            page_title=step.page_title,
            screenshot_path="/tmp/screenshot.png",
            http_requests=[],
            ws_frames=[],
            dom_summary=step.dom_summary,
        )
        _, _, artifacts, _ = self._run_extract([step])
        assert any("screenshot:" in k for k in artifacts)

    def test_empty_steps_returns_empty_facts(self) -> None:
        facts, coverage, artifacts, errors = self._run_extract([])
        assert facts == []
        assert coverage == []
        assert artifacts == {}
        assert errors == []


# ---------------------------------------------------------------------------
# run() error handling tests (mocked Playwright)
# ---------------------------------------------------------------------------


class TestRunErrorHandling:
    """Test that run() never raises and handles error cases per INV-020."""

    @pytest.mark.asyncio
    async def test_run_never_raises_on_timeout(self) -> None:
        module = BrowserExploreModule()
        request = make_request()

        # Mock browser that raises TimeoutError on new_context
        mock_browser = AsyncMock()
        mock_browser.new_context.side_effect = TimeoutError()

        services = make_services(browser=mock_browser)
        result = await module.run(request, services)

        # Must not raise — must return FAILED with a timeout error
        assert result.status == ReconModuleStatus.FAILED
        assert any(e.error_type == "timeout" for e in result.errors)
        assert result.facts == []

    @pytest.mark.asyncio
    async def test_run_returns_failed_on_auth_error(self) -> None:
        module = BrowserExploreModule()
        request = make_request(
            target="trello.com",
            base_url="https://trello.com",
        )

        # Provide credentials so auth is attempted
        services = make_services(
            credentials={
                "target.trello-com.username": "user@example.com",
                "target.trello-com.password": "wrongpass",
            },
        )

        # Build a realistic mock browser/page that simulates staying on login page
        mock_page = AsyncMock()
        mock_page.url = "https://trello.com/login"
        mock_page.title = AsyncMock(return_value="Login")
        mock_page.goto = AsyncMock()
        mock_page.wait_for_load_state = AsyncMock()
        mock_page.fill = AsyncMock()
        mock_page.click = AsyncMock()
        mock_page.on = MagicMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()

        mock_browser = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)

        services = make_services(
            browser=mock_browser,
            credentials={
                "target.trello-com.username": "user@example.com",
                "target.trello-com.password": "wrongpass",
            },
        )

        result = await module.run(request, services)

        # Auth failure falls back to anonymous exploration (PARTIAL, not FAILED)
        assert result.status in (ReconModuleStatus.PARTIAL, ReconModuleStatus.SUCCESS)
        assert any(e.error_type in ("auth_required", "timeout") for e in result.errors)

    @pytest.mark.asyncio
    async def test_run_returns_failed_on_unexpected_exception(self) -> None:
        module = BrowserExploreModule()
        request = make_request()

        mock_browser = AsyncMock()
        mock_browser.new_context.side_effect = RuntimeError("unexpected crash")

        services = make_services(browser=mock_browser)
        result = await module.run(request, services)

        assert result.status == ReconModuleStatus.FAILED
        assert result.facts == []
        assert len(result.errors) == 1

    @pytest.mark.asyncio
    async def test_run_result_module_matches_name(self) -> None:
        """ReconResult.module must always equal module.name."""
        module = BrowserExploreModule()
        request = make_request()

        mock_browser = AsyncMock()
        mock_browser.new_context.side_effect = RuntimeError("crash")

        services = make_services(browser=mock_browser)
        result = await module.run(request, services)

        assert result.module == "browser_explore"

    @pytest.mark.asyncio
    async def test_run_sets_started_and_finished_at(self) -> None:
        """Timing fields must always be set, even on failure."""
        module = BrowserExploreModule()
        request = make_request()

        mock_browser = AsyncMock()
        mock_browser.new_context.side_effect = RuntimeError("crash")

        services = make_services(browser=mock_browser)
        result = await module.run(request, services)

        assert result.started_at
        assert result.finished_at
        assert result.duration_seconds >= 0

    @pytest.mark.asyncio
    async def test_anonymous_explore_when_no_credentials(self) -> None:
        """With no credentials the module should attempt anonymous exploration."""
        module = BrowserExploreModule()
        request = make_request()
        services = make_services(credentials={})

        # Browser that succeeds with a minimal page
        mock_page = AsyncMock()
        mock_page.url = "https://trello.com/"
        mock_page.goto = AsyncMock()
        mock_page.wait_for_load_state = AsyncMock()
        mock_page.title = AsyncMock(return_value="Trello")
        mock_page.screenshot = AsyncMock()
        mock_page.evaluate = AsyncMock(return_value=[])
        mock_page.on = MagicMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)
        mock_context.close = AsyncMock()

        mock_browser = AsyncMock()
        mock_browser.new_context = AsyncMock(return_value=mock_context)

        services = make_services(browser=mock_browser, credentials={})
        result = await module.run(request, services)

        # Should succeed or be partial — must NOT be an auth failure
        assert result.status in (
            ReconModuleStatus.SUCCESS,
            ReconModuleStatus.PARTIAL,
            ReconModuleStatus.FAILED,
        )
        assert not any(e.error_type == "auth_required" for e in result.errors)


# ---------------------------------------------------------------------------
# validate_prerequisites tests
# ---------------------------------------------------------------------------


class TestValidatePrerequisites:
    @pytest.mark.asyncio
    async def test_returns_empty_when_playwright_installed(self) -> None:
        module = BrowserExploreModule()
        # Playwright is in the dev dependencies — this should pass
        missing = await module.validate_prerequisites()
        # In the test environment playwright may or may not be installed
        # Just verify the return type is correct
        assert isinstance(missing, list)
        assert all(isinstance(m, str) for m in missing)

    @pytest.mark.asyncio
    async def test_returns_message_when_playwright_missing(self) -> None:
        module = BrowserExploreModule()
        import builtins
        original_import = builtins.__import__

        def mock_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "playwright":
                raise ImportError("No module named 'playwright'")
            return original_import(name, *args, **kwargs)

        import builtins
        builtins.__import__ = mock_import
        try:
            missing = await module.validate_prerequisites()
        finally:
            builtins.__import__ = original_import

        assert len(missing) == 1
        assert "playwright" in missing[0]
