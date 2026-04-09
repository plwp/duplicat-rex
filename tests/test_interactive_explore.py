"""
Tests for InteractiveExploreModule (scripts/recon/interactive_explore.py).

All Playwright interactions are mocked — no real browser required.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

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
from scripts.recon.interactive_explore import (
    InteractiveElement,
    InteractionResult,
    InteractiveExploreModule,
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
        "module_config": {"urls_to_explore": ["https://trello.com/boards"]},
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


def make_button(text: str = "Create", visible: bool = True) -> InteractiveElement:
    return InteractiveElement(
        selector='button:has-text("Create")',
        element_type="button",
        text=text,
        visible=visible,
    )


def make_input(
    text: str = "email",
    input_type: str = "email",
    placeholder: str = "Enter email",
    visible: bool = True,
) -> InteractiveElement:
    return InteractiveElement(
        selector='input[type="email"]',
        element_type="input",
        text=text,
        visible=visible,
        input_type=input_type,
        placeholder=placeholder,
    )


def make_toggle(text: str = "Enable notifications") -> InteractiveElement:
    return InteractiveElement(
        selector='[role="switch"]',
        element_type="toggle",
        text=text,
        visible=True,
    )


def make_page_mock(
    url: str = "https://trello.com/boards",
    title: str = "Boards",
    buttons: list[dict[str, Any]] | None = None,
    inputs: list[dict[str, Any]] | None = None,
    has_modal: bool = False,
) -> MagicMock:
    """Build a mock Playwright page with configurable elements."""
    page = MagicMock()
    page.url = url
    page.title = AsyncMock(return_value=title)
    page.goto = AsyncMock()
    page.go_back = AsyncMock()
    page.screenshot = AsyncMock()
    page.keyboard = MagicMock()
    page.keyboard.press = AsyncMock()

    # click/fill
    page.click = AsyncMock()
    page.fill = AsyncMock()

    # DOM snapshot
    page.evaluate = AsyncMock(return_value="H1:Boards")

    # Modal detection
    modal_el = MagicMock()
    modal_el.is_visible = AsyncMock(return_value=has_modal)
    page.query_selector = AsyncMock(return_value=modal_el if has_modal else None)

    # Event listeners
    page.on = MagicMock()

    # Build element handles for buttons
    def _make_handle(el_type: str, attrs: dict[str, Any]) -> MagicMock:
        h = MagicMock()
        h.text_content = AsyncMock(return_value=attrs.get("text", ""))
        h.get_attribute = AsyncMock(side_effect=lambda k: attrs.get(k, None))
        h.is_visible = AsyncMock(return_value=attrs.get("visible", True))
        return h

    btn_handles = [_make_handle("button", b) for b in (buttons or [])]
    input_handles = [_make_handle("input", i) for i in (inputs or [])]

    async def query_selector_all(selector: str) -> list[MagicMock]:
        # Match button selectors (but not input selectors that mention type="button")
        if selector.startswith("button") or 'role="button"' in selector:
            return btn_handles
        # Match input/textarea/select selectors
        if selector.startswith("input") and "checkbox" not in selector:
            return input_handles
        return []

    page.query_selector_all = AsyncMock(side_effect=query_selector_all)

    return page


# ---------------------------------------------------------------------------
# Module property tests
# ---------------------------------------------------------------------------


class TestModuleProperties:
    def test_name(self) -> None:
        module = InteractiveExploreModule()
        assert module.name == "interactive_explore"

    def test_authority(self) -> None:
        module = InteractiveExploreModule()
        assert module.authority == Authority.AUTHORITATIVE

    def test_source_type(self) -> None:
        module = InteractiveExploreModule()
        assert module.source_type == SourceType.LIVE_APP

    def test_requires_credentials(self) -> None:
        module = InteractiveExploreModule()
        creds = module.requires_credentials
        assert isinstance(creds, list)
        assert len(creds) == 2
        joined = " ".join(creds)
        assert "{domain}" in joined
        assert "username" in joined
        assert "password" in joined


# ---------------------------------------------------------------------------
# Element discovery tests
# ---------------------------------------------------------------------------


class TestDiscoverButtons:
    @pytest.mark.asyncio
    async def test_discovers_buttons(self) -> None:
        module = InteractiveExploreModule()
        page = make_page_mock(
            buttons=[
                {"text": "Create board", "visible": True, "aria-label": ""},
                {"text": "Add card", "visible": True, "aria-label": ""},
            ]
        )
        elements = await module._discover_interactive_elements(page)
        button_elements = [e for e in elements if e.element_type == "button"]
        assert len(button_elements) == 2
        texts = {e.text for e in button_elements}
        assert "Create board" in texts
        assert "Add card" in texts

    @pytest.mark.asyncio
    async def test_discovers_hidden_buttons(self) -> None:
        """Hidden buttons are discovered but visible=False."""
        module = InteractiveExploreModule()
        page = make_page_mock(
            buttons=[{"text": "Hidden", "visible": False, "aria-label": ""}]
        )
        elements = await module._discover_interactive_elements(page)
        button_elements = [e for e in elements if e.element_type == "button"]
        assert len(button_elements) == 1
        assert not button_elements[0].visible


class TestDiscoverInputs:
    @pytest.mark.asyncio
    async def test_discovers_form_inputs(self) -> None:
        module = InteractiveExploreModule()
        page = make_page_mock(
            inputs=[
                {
                    "type": "email",
                    "placeholder": "Enter email",
                    "aria-label": "",
                    "name": "email",
                    "visible": True,
                },
                {
                    "type": "text",
                    "placeholder": "Board name",
                    "aria-label": "",
                    "name": "boardName",
                    "visible": True,
                },
            ]
        )
        elements = await module._discover_interactive_elements(page)
        input_elements = [e for e in elements if e.element_type == "input"]
        assert len(input_elements) == 2
        types = {e.input_type for e in input_elements}
        assert "email" in types
        assert "text" in types


# ---------------------------------------------------------------------------
# Exercise element tests
# ---------------------------------------------------------------------------


class TestExerciseButton:
    @pytest.mark.asyncio
    async def test_exercises_button_records_api_calls(self) -> None:
        module = InteractiveExploreModule()
        element = make_button("Archive")

        api_log: list[dict[str, Any]] = []
        expected_call = {"method": "POST", "path": "/1/boards/abc/archive", "status": 200}

        # Simulate an API call being added to the log when the button is clicked
        async def click_side_effect(*args: Any, **kwargs: Any) -> None:
            api_log.append(expected_call)

        page = MagicMock()
        page.url = "https://trello.com/boards"
        page.click = AsyncMock(side_effect=click_side_effect)
        page.evaluate = AsyncMock(return_value="H1:Boards")
        page.query_selector = AsyncMock(return_value=None)
        page.keyboard = MagicMock()
        page.keyboard.press = AsyncMock()
        page.screenshot = AsyncMock()
        page.query_selector_all = AsyncMock(return_value=[])

        result = await module._exercise_element(page, element, api_log, None, page.url)

        assert result.error is None
        assert result.api_calls == [expected_call]

    @pytest.mark.asyncio
    async def test_exercise_returns_error_on_timeout(self) -> None:
        module = InteractiveExploreModule()
        element = make_button("Star")

        page = MagicMock()
        page.url = "https://trello.com/boards"
        page.evaluate = AsyncMock(return_value="H1:Boards")
        page.click = AsyncMock(side_effect=Exception("Timeout 5000ms exceeded"))
        page.query_selector = AsyncMock(return_value=None)
        page.query_selector_all = AsyncMock(return_value=[])

        api_log: list[dict[str, Any]] = []
        result = await module._exercise_element(page, element, api_log, None, page.url)
        assert result.error is not None
        assert "Timeout" in result.error


# ---------------------------------------------------------------------------
# Modal detection tests
# ---------------------------------------------------------------------------


class TestDetectModal:
    @pytest.mark.asyncio
    async def test_detects_modal_when_present(self) -> None:
        page = MagicMock()
        modal_el = MagicMock()
        modal_el.is_visible = AsyncMock(return_value=True)
        page.query_selector = AsyncMock(return_value=modal_el)

        result = await InteractiveExploreModule._detect_modal(page)
        assert result is True

    @pytest.mark.asyncio
    async def test_no_modal_returns_false(self) -> None:
        page = MagicMock()
        page.query_selector = AsyncMock(return_value=None)

        result = await InteractiveExploreModule._detect_modal(page)
        assert result is False

    @pytest.mark.asyncio
    async def test_hidden_modal_returns_false(self) -> None:
        page = MagicMock()
        modal_el = MagicMock()
        modal_el.is_visible = AsyncMock(return_value=False)
        page.query_selector = AsyncMock(return_value=modal_el)

        result = await InteractiveExploreModule._detect_modal(page)
        assert result is False


# ---------------------------------------------------------------------------
# Destructive button safety tests
# ---------------------------------------------------------------------------


class TestSkipsDestructiveButtons:
    def test_skips_delete_button(self) -> None:
        element = make_button("Delete board")
        assert InteractiveExploreModule._is_destructive(element) is True

    def test_skips_remove_button(self) -> None:
        element = make_button("Remove member")
        assert InteractiveExploreModule._is_destructive(element) is True

    def test_skips_payment_button(self) -> None:
        element = make_button("Pay now")
        assert InteractiveExploreModule._is_destructive(element) is True

    def test_skips_send_email_button(self) -> None:
        element = make_button("Send email")
        assert InteractiveExploreModule._is_destructive(element) is True

    def test_safe_button_not_skipped(self) -> None:
        element = make_button("Create board")
        assert InteractiveExploreModule._is_destructive(element) is False

    def test_safe_archive_button_not_skipped(self) -> None:
        # "archive" is not in deny list (it's reversible)
        element = make_button("Archive board")
        assert InteractiveExploreModule._is_destructive(element) is False

    def test_destructive_via_aria_label(self) -> None:
        element = InteractiveElement(
            selector="button",
            element_type="button",
            text="",
            visible=True,
            aria_label="Delete this item",
        )
        assert InteractiveExploreModule._is_destructive(element) is True


# ---------------------------------------------------------------------------
# Test value generation
# ---------------------------------------------------------------------------


class TestTestValue:
    def test_email_input(self) -> None:
        el = make_input(input_type="email", placeholder="email")
        assert InteractiveExploreModule._test_value(el) == "test@example.com"

    def test_password_input(self) -> None:
        el = make_input(input_type="password", placeholder="password", text="password")
        assert "Password" in InteractiveExploreModule._test_value(el) or \
               "password" in InteractiveExploreModule._test_value(el).lower()

    def test_text_input_with_name_placeholder(self) -> None:
        el = make_input(input_type="text", placeholder="Board name", text="name")
        val = InteractiveExploreModule._test_value(el)
        assert "Name" in val or "name" in val.lower()

    def test_url_input(self) -> None:
        el = make_input(input_type="url", placeholder="website url", text="website")
        assert InteractiveExploreModule._test_value(el).startswith("https://")

    def test_number_input(self) -> None:
        el = make_input(input_type="number", placeholder="count", text="count")
        assert InteractiveExploreModule._test_value(el) == "1"

    def test_generic_fallback(self) -> None:
        el = make_input(input_type="text", placeholder="something", text="something")
        assert InteractiveExploreModule._test_value(el) == "Test Value"


# ---------------------------------------------------------------------------
# Fact generation tests
# ---------------------------------------------------------------------------


class TestGeneratesUIFacts:
    def test_button_click_opens_modal(self) -> None:
        module = InteractiveExploreModule()
        element = make_button("Create board")
        result = InteractionResult(
            element=element,
            modal_opened=True,
            dom_changed=True,
            api_calls=[],
        )
        facts = module._facts_from_result(
            result,
            page_url="https://trello.com/boards",
            feature="boards",
            run_id=str(uuid.uuid4()),
            now="2025-01-01T00:00:00+00:00",
        )
        assert len(facts) >= 1
        ui_facts = [f for f in facts if f.category == FactCategory.UI_COMPONENT]
        assert len(ui_facts) == 1
        assert "modal" in ui_facts[0].claim.lower()
        assert "Create board" in ui_facts[0].claim

    def test_navigation_fact_when_url_changes(self) -> None:
        module = InteractiveExploreModule()
        element = make_button("Go to settings")
        result = InteractionResult(
            element=element,
            url_changed=True,
            new_url="https://trello.com/settings",
            api_calls=[],
        )
        facts = module._facts_from_result(
            result,
            page_url="https://trello.com/boards",
            feature="boards",
            run_id=str(uuid.uuid4()),
            now="2025-01-01T00:00:00+00:00",
        )
        flow_facts = [f for f in facts if f.category == FactCategory.USER_FLOW]
        assert len(flow_facts) >= 1
        assert "settings" in flow_facts[0].claim.lower()

    def test_no_facts_for_errored_interaction(self) -> None:
        module = InteractiveExploreModule()
        element = make_button("Broken button")
        result = InteractionResult(element=element, error="Element not found")
        facts = module._facts_from_result(
            result,
            page_url="https://trello.com/boards",
            feature="boards",
            run_id=str(uuid.uuid4()),
            now="2025-01-01T00:00:00+00:00",
        )
        assert facts == []

    def test_toggle_without_api_call_generates_business_rule(self) -> None:
        module = InteractiveExploreModule()
        element = make_toggle("Enable notifications")
        result = InteractionResult(
            element=element,
            dom_changed=True,
            api_calls=[],
        )
        facts = module._facts_from_result(
            result,
            page_url="https://trello.com/settings",
            feature="settings",
            run_id=str(uuid.uuid4()),
            now="2025-01-01T00:00:00+00:00",
        )
        biz_facts = [f for f in facts if f.category == FactCategory.BUSINESS_RULE]
        assert len(biz_facts) == 1
        assert "Toggling" in biz_facts[0].claim


class TestGeneratesApiFactsFromFormSubmit:
    def test_form_submit_generates_api_and_user_flow_facts(self) -> None:
        module = InteractiveExploreModule()
        element = make_input("Board name", "text", "Board name")
        result = InteractionResult(
            element=element,
            api_calls=[
                {"method": "POST", "path": "/1/boards", "status": 200}
            ],
            dom_changed=True,
        )
        facts = module._facts_from_result(
            result,
            page_url="https://trello.com/boards",
            feature="boards",
            run_id=str(uuid.uuid4()),
            now="2025-01-01T00:00:00+00:00",
        )
        api_facts = [f for f in facts if f.category == FactCategory.API_ENDPOINT]
        flow_facts = [f for f in facts if f.category == FactCategory.USER_FLOW]

        assert len(api_facts) >= 1
        assert "POST" in api_facts[0].claim
        assert "/1/boards" in api_facts[0].claim

        assert len(flow_facts) >= 1

    def test_button_click_api_generates_endpoint_fact(self) -> None:
        module = InteractiveExploreModule()
        element = make_button("Star board")
        result = InteractionResult(
            element=element,
            api_calls=[
                {"method": "PUT", "path": "/1/boards/abc/starred", "status": 200}
            ],
        )
        facts = module._facts_from_result(
            result,
            page_url="https://trello.com/boards",
            feature="boards",
            run_id=str(uuid.uuid4()),
            now="2025-01-01T00:00:00+00:00",
        )
        api_facts = [f for f in facts if f.category == FactCategory.API_ENDPOINT]
        assert len(api_facts) == 1
        assert "Star board" in api_facts[0].claim
        assert "PUT" in api_facts[0].claim
        assert api_facts[0].structured_data["trigger"] == "button_click"

    def test_api_fact_authority_is_authoritative(self) -> None:
        module = InteractiveExploreModule()
        element = make_button("Save")
        result = InteractionResult(
            element=element,
            api_calls=[{"method": "PATCH", "path": "/api/settings", "status": 204}],
        )
        facts = module._facts_from_result(
            result,
            page_url="https://trello.com/settings",
            feature="settings",
            run_id=str(uuid.uuid4()),
            now="2025-01-01T00:00:00+00:00",
        )
        for f in facts:
            assert f.authority == Authority.AUTHORITATIVE
            assert f.source_type == SourceType.LIVE_APP
            assert f.module_name == "interactive_explore"


# ---------------------------------------------------------------------------
# Full run integration (mocked browser)
# ---------------------------------------------------------------------------


class TestRunWithMockedBrowser:
    @pytest.mark.asyncio
    async def test_run_succeeds_with_mocked_browser(self) -> None:
        """run() should return SUCCESS with facts when browser is mocked."""
        module = InteractiveExploreModule()
        request = make_request(
            module_config={"urls_to_explore": ["https://trello.com/boards"]}
        )

        # Build a minimal mock browser/context/page
        page = MagicMock()
        page.url = "https://trello.com/boards"
        page.goto = AsyncMock()
        page.go_back = AsyncMock()
        page.click = AsyncMock()
        page.fill = AsyncMock()
        page.evaluate = AsyncMock(return_value="H1:Boards")
        page.on = MagicMock()
        page.screenshot = AsyncMock()
        page.keyboard = MagicMock()
        page.keyboard.press = AsyncMock()

        # Return a button
        btn_handle = MagicMock()
        btn_handle.text_content = AsyncMock(return_value="Create board")
        btn_handle.get_attribute = AsyncMock(return_value=None)
        btn_handle.is_visible = AsyncMock(return_value=True)

        modal_el = MagicMock()
        modal_el.is_visible = AsyncMock(return_value=True)

        async def query_selector_all(selector: str) -> list[MagicMock]:
            if "button" in selector:
                return [btn_handle]
            return []

        page.query_selector_all = AsyncMock(side_effect=query_selector_all)
        page.query_selector = AsyncMock(return_value=modal_el)

        context = MagicMock()
        context.new_page = AsyncMock(return_value=page)
        context.close = AsyncMock()

        browser = MagicMock()
        browser.new_context = AsyncMock(return_value=context)

        services = make_services(browser=browser)
        result = await module.run(request, services)

        assert result.module == "interactive_explore"
        assert result.status in (ReconModuleStatus.SUCCESS, ReconModuleStatus.PARTIAL)
        assert len(result.facts) > 0
        for f in result.facts:
            assert f.module_name == "interactive_explore"
            assert f.authority == Authority.AUTHORITATIVE

    @pytest.mark.asyncio
    async def test_run_captures_errors_does_not_raise(self) -> None:
        """run() must not raise even if browser setup fails (INV-020)."""
        module = InteractiveExploreModule()
        request = make_request()

        browser = MagicMock()
        browser.new_context = AsyncMock(side_effect=Exception("Browser crashed"))
        services = make_services(browser=browser)

        result = await module.run(request, services)
        assert result.status == ReconModuleStatus.FAILED
        assert len(result.errors) > 0
