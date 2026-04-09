"""
Tests for VerificationGate — automated post-implementation verification.

Covers:
  - test_api_check_passes_on_200 — mock httpx returning 200
  - test_api_check_fails_on_404 — endpoint not found
  - test_page_check_fails_on_placeholder — page has "Coming soon"
  - test_page_check_passes_on_real_content — page has actual UI
  - test_element_check_finds_button — button exists
  - test_element_check_fails_missing — expected button not on page
  - test_verify_all_returns_results_per_ticket
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scripts.model_ticket_generator import FieldSpec, TicketSpec
from scripts.verification_gate import (
    CheckResult,
    VerificationGate,
    VerificationResult,
    _detect_placeholder,
    _json_has_key,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def make_ticket(
    ticket_id: str = "board-create",
    operation: str = "create",
    api_endpoint: str = "/api/boards",
    api_method: str = "POST",
    ui_location: str = "",
    ui_components: list[str] | None = None,
    response_fields: list[FieldSpec] | None = None,
) -> TicketSpec:
    return TicketSpec(
        id=ticket_id,
        title=f"Test ticket {ticket_id}",
        entity="Board",
        operation=operation,
        priority=2,
        api_method=api_method,
        api_endpoint=api_endpoint,
        ui_location=ui_location,
        ui_components=ui_components or [],
        response_fields=response_fields or [],
    )


def _mock_httpx_response(status_code: int, json_body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"content-type": "application/json"} if json_body else {}
    resp.json.return_value = json_body or {}
    return resp


# ---------------------------------------------------------------------------
# Unit tests — _detect_placeholder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected_match",
    [
        ("Coming soon", "Coming soon"),
        ("This page is TODO", "TODO"),
        ("Not implemented yet", "Not implemented"),
        ("This is a placeholder page", "placeholder"),
        ("Under construction", "Under construction"),
        ("Hello world", None),
        ("Real content with a button", None),
    ],
)
def test_detect_placeholder(text: str, expected_match: str | None) -> None:
    result = _detect_placeholder(text)
    if expected_match is None:
        assert result is None
    else:
        assert result is not None
        assert expected_match.lower() in result.lower()


# ---------------------------------------------------------------------------
# Unit tests — _json_has_key
# ---------------------------------------------------------------------------


def test_json_has_key_flat_dict() -> None:
    assert _json_has_key({"id": 1, "title": "test"}, "id") is True
    assert _json_has_key({"id": 1, "title": "test"}, "missing") is False


def test_json_has_key_nested() -> None:
    body = {"data": {"board": {"id": 1, "title": "t"}}}
    assert _json_has_key(body, "id") is True
    assert _json_has_key(body, "nope") is False


def test_json_has_key_in_list() -> None:
    body = [{"id": 1}, {"id": 2}]
    assert _json_has_key(body, "id") is True
    assert _json_has_key(body, "x") is False


# ---------------------------------------------------------------------------
# API check tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_check_passes_on_200() -> None:
    gate = VerificationGate(clone_url="http://localhost:3000")
    ticket = make_ticket(api_endpoint="/api/boards", api_method="GET", operation="list")

    mock_resp = _mock_httpx_response(200)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        result = await gate._verify_api(ticket)

    assert len(result) >= 1
    exists_check = result[0]
    assert exists_check.passed is True
    assert "200" in exists_check.evidence


@pytest.mark.asyncio
async def test_api_check_fails_on_404() -> None:
    gate = VerificationGate(clone_url="http://localhost:3000")
    ticket = make_ticket(api_endpoint="/api/boards", api_method="GET", operation="list")

    mock_resp = _mock_httpx_response(404)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        result = await gate._verify_api(ticket)

    exists_check = result[0]
    assert exists_check.passed is False
    assert "404" in exists_check.evidence


@pytest.mark.asyncio
async def test_api_check_includes_response_field_checks() -> None:
    gate = VerificationGate(clone_url="http://localhost:3000")
    ticket = make_ticket(
        api_endpoint="/api/boards",
        api_method="POST",
        operation="create",
        response_fields=[FieldSpec(name="id", field_type="string", required=True)],
    )

    mock_resp = _mock_httpx_response(201, json_body={"id": "abc", "title": "Test"})

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        result = await gate._verify_api(ticket)

    field_checks = [c for c in result if "field" in c.criterion.lower()]
    assert len(field_checks) >= 1
    assert all(c.passed for c in field_checks)


@pytest.mark.asyncio
async def test_api_check_fails_missing_response_field() -> None:
    gate = VerificationGate(clone_url="http://localhost:3000")
    ticket = make_ticket(
        api_endpoint="/api/boards",
        api_method="POST",
        operation="create",
        response_fields=[FieldSpec(name="missingField", field_type="string", required=True)],
    )

    mock_resp = _mock_httpx_response(201, json_body={"id": "abc"})

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        result = await gate._verify_api(ticket)

    field_checks = [c for c in result if "missingField" in c.criterion]
    assert len(field_checks) == 1
    assert field_checks[0].passed is False


# ---------------------------------------------------------------------------
# Page check tests
# ---------------------------------------------------------------------------


def _make_mock_playwright_page(
    http_status: int = 200,
    body_text: str = "Real content",
    interactive_count: int = 1,
) -> tuple:
    """Return (mock_playwright_ctx, mock_page) for patching async_playwright."""

    mock_page = AsyncMock()

    mock_response = MagicMock()
    mock_response.status = http_status

    mock_page.goto = AsyncMock(return_value=mock_response)
    mock_page.inner_text = AsyncMock(return_value=body_text)
    mock_page.screenshot = AsyncMock()

    # Locator for interactive elements
    interactive_locator = AsyncMock()
    interactive_locator.count = AsyncMock(return_value=interactive_count)
    mock_page.locator = MagicMock(return_value=interactive_locator)

    mock_context = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)

    mock_browser = AsyncMock()
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_browser.close = AsyncMock()

    mock_chromium = AsyncMock()
    mock_chromium.launch = AsyncMock(return_value=mock_browser)

    mock_pw_instance = AsyncMock()
    mock_pw_instance.chromium = mock_chromium
    mock_pw_instance.__aenter__ = AsyncMock(return_value=mock_pw_instance)
    mock_pw_instance.__aexit__ = AsyncMock(return_value=False)

    mock_async_playwright = MagicMock(return_value=mock_pw_instance)

    return mock_async_playwright, mock_page


@pytest.mark.asyncio
async def test_page_check_fails_on_placeholder() -> None:
    gate = VerificationGate(clone_url="http://localhost:3000")
    ticket = make_ticket(ui_location="/boards")

    mock_async_pw, _ = _make_mock_playwright_page(
        http_status=200,
        body_text="Coming soon — we're working on it!",
        interactive_count=0,
    )

    with patch("scripts.verification_gate.async_playwright", mock_async_pw):
        result = await gate._verify_page(ticket)

    placeholder_checks = [c for c in result if "placeholder" in c.criterion.lower()]
    assert len(placeholder_checks) >= 1
    assert placeholder_checks[0].passed is False
    assert "Coming soon" in placeholder_checks[0].evidence


@pytest.mark.asyncio
async def test_page_check_passes_on_real_content() -> None:
    gate = VerificationGate(clone_url="http://localhost:3000")
    ticket = make_ticket(ui_location="/boards")

    mock_async_pw, _ = _make_mock_playwright_page(
        http_status=200,
        body_text="My Boards — Create Board",
        interactive_count=3,
    )

    with patch("scripts.verification_gate.async_playwright", mock_async_pw):
        result = await gate._verify_page(ticket)

    load_check = result[0]
    assert load_check.passed is True

    placeholder_check = next(c for c in result if "placeholder" in c.criterion.lower())
    assert placeholder_check.passed is True


@pytest.mark.asyncio
async def test_page_check_fails_on_404() -> None:
    gate = VerificationGate(clone_url="http://localhost:3000")
    ticket = make_ticket(ui_location="/boards")

    mock_async_pw, _ = _make_mock_playwright_page(http_status=404, body_text="Not Found")

    with patch("scripts.verification_gate.async_playwright", mock_async_pw):
        result = await gate._verify_page(ticket)

    load_check = result[0]
    assert load_check.passed is False
    assert "404" in load_check.evidence


# ---------------------------------------------------------------------------
# Element check tests
# ---------------------------------------------------------------------------


def _make_element_playwright(
    http_status: int = 200,
    data_attr_count: int = 0,
    button_count: int = 0,
    text_count: int = 0,
    button_disabled: str | None = None,
) -> MagicMock:
    """Return mock async_playwright for element-level tests."""

    # data-attr locator
    attr_locator = AsyncMock()
    attr_locator.count = AsyncMock(return_value=data_attr_count)

    # button role locator
    btn_locator = AsyncMock()
    btn_locator.count = AsyncMock(return_value=button_count)
    btn_first = AsyncMock()
    btn_first.get_attribute = AsyncMock(return_value=button_disabled)
    btn_locator.first = btn_first

    # text locator
    text_locator = AsyncMock()
    text_locator.count = AsyncMock(return_value=text_count)

    mock_page = AsyncMock()
    mock_response = MagicMock()
    mock_response.status = http_status
    mock_page.goto = AsyncMock(return_value=mock_response)

    def _locator(selector: str) -> AsyncMock:
        return attr_locator

    mock_page.locator = MagicMock(side_effect=_locator)

    def _get_by_role(role: str, **kwargs: object) -> AsyncMock:
        return btn_locator

    mock_page.get_by_role = MagicMock(side_effect=_get_by_role)

    def _get_by_text(pattern: object) -> AsyncMock:
        return text_locator

    mock_page.get_by_text = MagicMock(side_effect=_get_by_text)

    mock_context = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_browser = AsyncMock()
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_browser.close = AsyncMock()

    mock_chromium = AsyncMock()
    mock_chromium.launch = AsyncMock(return_value=mock_browser)

    mock_pw_instance = AsyncMock()
    mock_pw_instance.chromium = mock_chromium
    mock_pw_instance.__aenter__ = AsyncMock(return_value=mock_pw_instance)
    mock_pw_instance.__aexit__ = AsyncMock(return_value=False)

    return MagicMock(return_value=mock_pw_instance)


@pytest.mark.asyncio
async def test_element_check_finds_button() -> None:
    gate = VerificationGate(clone_url="http://localhost:3000")
    ticket = make_ticket(
        ui_location="/boards",
        ui_components=["CreateBoardButton"],
    )

    # data-attr finds nothing, but button role matches
    mock_async_pw = _make_element_playwright(
        http_status=200,
        data_attr_count=0,
        button_count=1,
        button_disabled=None,
    )

    with patch("scripts.verification_gate.async_playwright", mock_async_pw):
        result = await gate._verify_elements(ticket)

    assert len(result) == 1
    assert result[0].passed is True
    assert "button" in result[0].evidence.lower()


@pytest.mark.asyncio
async def test_element_check_fails_missing() -> None:
    gate = VerificationGate(clone_url="http://localhost:3000")
    ticket = make_ticket(
        ui_location="/boards",
        ui_components=["BoardGrid"],
    )

    mock_async_pw = _make_element_playwright(
        http_status=200,
        data_attr_count=0,
        button_count=0,
        text_count=0,
    )

    with patch("scripts.verification_gate.async_playwright", mock_async_pw):
        result = await gate._verify_elements(ticket)

    assert len(result) == 1
    assert result[0].passed is False
    assert "BoardGrid" in result[0].criterion


@pytest.mark.asyncio
async def test_element_check_finds_via_data_attr() -> None:
    gate = VerificationGate(clone_url="http://localhost:3000")
    ticket = make_ticket(
        ui_location="/boards",
        ui_components=["BoardCard"],
    )

    mock_async_pw = _make_element_playwright(
        http_status=200,
        data_attr_count=2,  # found via data-testid
    )

    with patch("scripts.verification_gate.async_playwright", mock_async_pw):
        result = await gate._verify_elements(ticket)

    assert len(result) == 1
    assert result[0].passed is True
    assert "data-testid" in result[0].evidence


# ---------------------------------------------------------------------------
# verify_ticket integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_ticket_no_checks_passes() -> None:
    """A ticket with no api_endpoint, ui_location, or ui_components returns passed."""
    gate = VerificationGate(clone_url="http://localhost:3000")
    ticket = TicketSpec(
        id="auth-setup",
        title="Auth setup",
        entity="Auth",
        operation="setup",
        priority=1,
    )
    result = await gate.verify_ticket(ticket)
    assert result.passed is True
    assert result.ticket_id == "auth-setup"


@pytest.mark.asyncio
async def test_verify_ticket_api_only() -> None:
    """verify_ticket with only api_endpoint runs API checks only."""
    gate = VerificationGate(clone_url="http://localhost:3000")
    ticket = make_ticket(api_endpoint="/api/boards", api_method="GET", operation="list")

    mock_resp = _mock_httpx_response(200)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        result = await gate.verify_ticket(ticket)

    assert result.ticket_id == "board-create"
    assert all(c.method == "api_check" for c in result.checks)
    assert result.passed is True


# ---------------------------------------------------------------------------
# verify_all — one result per ticket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_all_returns_results_per_ticket() -> None:
    gate = VerificationGate(clone_url="http://localhost:3000")

    tickets = [
        TicketSpec(id=f"ticket-{i}", title=f"T{i}", entity="X", operation="op", priority=1)
        for i in range(3)
    ]

    results = await gate.verify_all(tickets)

    assert len(results) == 3
    ids = [r.ticket_id for r in results]
    assert ids == ["ticket-0", "ticket-1", "ticket-2"]
    # All have no verifiable criteria, so they pass
    assert all(r.passed for r in results)


@pytest.mark.asyncio
async def test_verify_all_preserves_order() -> None:
    """Results come back in the same order as the input tickets."""
    gate = VerificationGate(clone_url="http://localhost:3000")

    tickets = [
        TicketSpec(id=f"t{i}", title=f"T{i}", entity="X", operation="op", priority=1)
        for i in range(5)
    ]

    results = await gate.verify_all(tickets)

    for i, r in enumerate(results):
        assert r.ticket_id == f"t{i}"


# ---------------------------------------------------------------------------
# VerificationResult and CheckResult dataclasses
# ---------------------------------------------------------------------------


def test_check_result_defaults() -> None:
    cr = CheckResult(
        criterion="endpoint exists",
        passed=True,
        method="api_check",
        evidence="HTTP 200",
    )
    assert cr.screenshot is None


def test_verification_result_defaults() -> None:
    vr = VerificationResult(ticket_id="x", passed=True)
    assert vr.checks == []
    assert vr.screenshots == []
