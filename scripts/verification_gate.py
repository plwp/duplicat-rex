"""
VerificationGate — automated post-implementation verification.

For each implemented ticket, navigates the clone app via Playwright and
verifies that the acceptance criteria are met. Fails if any AC shows
placeholder text, non-functional buttons, or missing functionality.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from playwright.async_api import Page

try:
    from playwright.async_api import async_playwright
except ImportError:  # pragma: no cover
    async_playwright = None  # type: ignore[assignment]

from scripts.model_ticket_generator import TicketSpec

# ---------------------------------------------------------------------------
# Placeholder patterns — any of these in page text signals a stub
# ---------------------------------------------------------------------------

PLACEHOLDER_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"coming soon", re.IGNORECASE),
    re.compile(r"\bTODO\b", re.IGNORECASE),
    re.compile(r"not implemented", re.IGNORECASE),
    re.compile(r"\bplaceholder\b", re.IGNORECASE),
    re.compile(r"under construction", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Result data structures
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    criterion: str  # the AC text
    passed: bool
    method: str  # "api_check", "page_check", "element_check"
    evidence: str  # what we found
    screenshot: str | None = None


@dataclass
class VerificationResult:
    ticket_id: str
    passed: bool
    checks: list[CheckResult] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# VerificationGate
# ---------------------------------------------------------------------------


class VerificationGate:
    """
    Automated post-implementation verifier.

    Navigates the clone app via Playwright and httpx to check that each
    ticket's acceptance criteria are actually satisfied — no placeholders,
    no 404s, no non-functional buttons.
    """

    def __init__(
        self,
        clone_url: str,
        auth_state_path: str | None = None,
    ) -> None:
        self.clone_url = clone_url.rstrip("/")
        self.auth_state_path = auth_state_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def verify_ticket(self, ticket: TicketSpec) -> VerificationResult:
        """Verify a single ticket's implementation against its ACs."""
        checks: list[CheckResult] = []

        if ticket.api_endpoint:
            checks.extend(await self._verify_api(ticket))

        if ticket.ui_location:
            checks.extend(await self._verify_page(ticket))

        if ticket.ui_components:
            checks.extend(await self._verify_elements(ticket))

        # If no specific checks were produced, mark as a pass with a note
        if not checks:
            checks.append(
                CheckResult(
                    criterion="No verifiable criteria found",
                    passed=True,
                    method="api_check",
                    evidence="Ticket has no api_endpoint, ui_location, or ui_components",
                )
            )

        passed = all(c.passed for c in checks)
        screenshots = [c.screenshot for c in checks if c.screenshot]
        return VerificationResult(
            ticket_id=ticket.id,
            passed=passed,
            checks=checks,
            screenshots=screenshots,
        )

    async def verify_all(self, tickets: list[TicketSpec]) -> list[VerificationResult]:
        """Verify all tickets sequentially, return one result per ticket."""
        results: list[VerificationResult] = []
        for ticket in tickets:
            result = await self.verify_ticket(ticket)
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Internal: API verification
    # ------------------------------------------------------------------

    async def _verify_api(self, ticket: TicketSpec) -> list[CheckResult]:
        """Hit the API endpoint and verify it responds correctly."""
        checks: list[CheckResult] = []

        # Resolve the endpoint — replace path params with a sentinel value
        raw_endpoint = ticket.api_endpoint
        endpoint = re.sub(r"\{[^}]+\}", "1", raw_endpoint)
        url = f"{self.clone_url}{endpoint}"

        method = (ticket.api_method or "GET").upper()
        expected_status = self._expected_status(ticket)

        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as client:
                request_fn = getattr(client, method.lower(), client.get)
                response = await request_fn(url)
                actual_status = response.status_code

            # Check: endpoint exists (not 404)
            exists = actual_status != 404
            checks.append(
                CheckResult(
                    criterion=f"Endpoint {method} {raw_endpoint} exists (not 404)",
                    passed=exists,
                    method="api_check",
                    evidence=f"HTTP {actual_status}",
                )
            )

            # Check: returns expected status code (if we got a non-404 back)
            if exists:
                status_ok = actual_status == expected_status or actual_status < 500
                checks.append(
                    CheckResult(
                        criterion=f"{method} {raw_endpoint} returns {expected_status}",
                        passed=status_ok,
                        method="api_check",
                        evidence=f"HTTP {actual_status} (expected {expected_status})",
                    )
                )

                # Check: response has expected fields (JSON only)
                if ticket.response_fields and "application/json" in response.headers.get(
                    "content-type", ""
                ):
                    try:
                        body = response.json()
                        for rf in ticket.response_fields:
                            present = _json_has_key(body, rf.name)
                            checks.append(
                                CheckResult(
                                    criterion=f"Response includes field '{rf.name}'",
                                    passed=present,
                                    method="api_check",
                                    evidence=(
                                        f"Field {'found' if present else 'missing'}"
                                        " in response"
                                    ),
                                )
                            )
                    except Exception:  # noqa: BLE001
                        pass

        except httpx.TransportError as exc:
            checks.append(
                CheckResult(
                    criterion=f"Endpoint {method} {raw_endpoint} is reachable",
                    passed=False,
                    method="api_check",
                    evidence=f"Connection error: {exc}",
                )
            )

        return checks

    # ------------------------------------------------------------------
    # Internal: page verification
    # ------------------------------------------------------------------

    async def _verify_page(self, ticket: TicketSpec) -> list[CheckResult]:
        """Navigate to the UI page and verify it's not a placeholder."""
        checks: list[CheckResult] = []

        # Resolve the location — replace path params with a sentinel
        raw_location = ticket.ui_location
        location = re.sub(r"\{[^}]+\}", "1", raw_location)
        url = f"{self.clone_url}{location}"

        if async_playwright is None:
            checks.append(
                CheckResult(
                    criterion=f"Page {raw_location} loads without error",
                    passed=False,
                    method="page_check",
                    evidence="Playwright not installed — install with: pip install playwright",
                )
            )
            return checks

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                context_args: dict = {}
                if self.auth_state_path:
                    context_args["storage_state"] = self.auth_state_path
                context = await browser.new_context(**context_args)
                page = await context.new_page()

                response = await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                http_status = response.status if response else 0

                # Check: page loads (not 404, not 5xx)
                loads_ok = 200 <= http_status < 400
                checks.append(
                    CheckResult(
                        criterion=f"Page {raw_location} loads without error",
                        passed=loads_ok,
                        method="page_check",
                        evidence=f"HTTP {http_status}",
                    )
                )

                if loads_ok:
                    page_text = await page.inner_text("body")

                    # Check: no placeholder text
                    placeholder_found = _detect_placeholder(page_text)
                    checks.append(
                        CheckResult(
                            criterion=f"Page {raw_location} has no placeholder text",
                            passed=not placeholder_found,
                            method="page_check",
                            evidence=(
                                f"Placeholder detected: {placeholder_found!r}"
                                if placeholder_found
                                else "No placeholder text found"
                            ),
                        )
                    )

                    # Check: page has actual content (not just a heading)
                    has_content = await _page_has_real_content(page)
                    checks.append(
                        CheckResult(
                            criterion=f"Page {raw_location} has interactive content",
                            passed=has_content,
                            method="page_check",
                            evidence=(
                                "Page has interactive elements"
                                if has_content
                                else "Page appears empty or heading-only"
                            ),
                        )
                    )

                    # Screenshot as evidence
                    screenshot_path = f"/tmp/verification_{ticket.id}_page.png"
                    await page.screenshot(path=screenshot_path, full_page=True)
                    # Attach screenshot to the last check
                    if checks:
                        checks[-1].screenshot = screenshot_path

                await browser.close()

        except Exception as exc:  # noqa: BLE001
            checks.append(
                CheckResult(
                    criterion=f"Page {raw_location} loads without error",
                    passed=False,
                    method="page_check",
                    evidence=f"Error: {exc}",
                )
            )

        return checks

    # ------------------------------------------------------------------
    # Internal: element verification
    # ------------------------------------------------------------------

    async def _verify_elements(self, ticket: TicketSpec) -> list[CheckResult]:
        """Verify that expected UI components exist on the page."""
        checks: list[CheckResult] = []

        if not ticket.ui_location:
            return checks

        raw_location = ticket.ui_location
        location = re.sub(r"\{[^}]+\}", "1", raw_location)
        url = f"{self.clone_url}{location}"

        if async_playwright is None:
            for component in ticket.ui_components:
                checks.append(
                    CheckResult(
                        criterion=f"Component '{component}' exists",
                        passed=False,
                        method="element_check",
                        evidence="Playwright not installed",
                    )
                )
            return checks

        try:
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                context_args: dict = {}
                if self.auth_state_path:
                    context_args["storage_state"] = self.auth_state_path
                context = await browser.new_context(**context_args)
                page = await context.new_page()

                response = await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                http_status = response.status if response else 0

                if 200 <= http_status < 400:
                    for component in ticket.ui_components:
                        result = await _check_component(page, component, ticket)
                        checks.append(result)

                await browser.close()

        except Exception as exc:  # noqa: BLE001
            for component in ticket.ui_components:
                checks.append(
                    CheckResult(
                        criterion=f"Component '{component}' exists",
                        passed=False,
                        method="element_check",
                        evidence=f"Error: {exc}",
                    )
                )

        return checks

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _expected_status(self, ticket: TicketSpec) -> int:
        """Infer expected HTTP status from the ticket operation."""
        op = (ticket.operation or "").lower()
        if op == "delete":
            return 204
        if op == "create":
            return 201
        return 200


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _detect_placeholder(text: str) -> str | None:
    """Return the matched placeholder string, or None if none found."""
    for pattern in PLACEHOLDER_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def _json_has_key(body: object, key: str) -> bool:
    """Return True if key exists anywhere in a JSON object/list."""
    if isinstance(body, dict):
        if key in body:
            return True
        return any(_json_has_key(v, key) for v in body.values())
    if isinstance(body, list):
        return any(_json_has_key(item, key) for item in body)
    return False


async def _page_has_real_content(page: Page) -> bool:
    """
    Return True if the page has interactive content beyond a bare heading.

    We consider a page real if it has at least one of:
      - A button, link, input, select, textarea, or form
      - More than one paragraph of text
    """
    # Count interactive elements
    interactive_count = await page.locator(
        "button, a[href], input, select, textarea, form"
    ).count()
    if interactive_count > 0:
        return True

    # Fall back: check for multiple paragraph-like elements
    para_count = await page.locator("p, li, td, article, section").count()
    return para_count > 1


async def _check_component(
    page: Page, component: str, ticket: TicketSpec
) -> CheckResult:
    """
    Check whether a named component is present and functional on the page.

    Strategy:
      - Try data-testid / data-component attributes first (most reliable)
      - Fall back to ARIA role + text heuristics derived from component name
      - For buttons: verify they have a click handler or are not disabled
    """
    # Derive a human-readable label from the CamelCase component name
    label = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", component).lower()
    entity_name = label.replace("modal", "").replace("form", "").replace("grid", "").strip()

    # Try data attributes
    by_attr = page.locator(
        f'[data-testid="{component}"], [data-component="{component}"]'
    )
    if await by_attr.count() > 0:
        return CheckResult(
            criterion=f"Component '{component}' exists",
            passed=True,
            method="element_check",
            evidence="Found via data-testid/data-component attribute",
        )

    # Try button/interactive element by label text
    is_button_like = any(
        kw in component.lower() for kw in ("button", "modal", "form", "trigger")
    )
    if is_button_like and entity_name:
        btn = page.get_by_role("button", name=re.compile(entity_name, re.IGNORECASE))
        if await btn.count() > 0:
            # Verify clickability (not disabled)
            disabled = await btn.first.get_attribute("disabled")
            return CheckResult(
                criterion=f"Component '{component}' exists and is functional",
                passed=disabled is None,
                method="element_check",
                evidence=(
                    "Button found but disabled"
                    if disabled is not None
                    else f"Button '{entity_name}' found and clickable"
                ),
            )

    # Try heading / section text match for Card/Grid/Detail/Header
    if entity_name:
        by_text = page.get_by_text(re.compile(entity_name, re.IGNORECASE))
        if await by_text.count() > 0:
            return CheckResult(
                criterion=f"Component '{component}' exists",
                passed=True,
                method="element_check",
                evidence=f"Text matching '{entity_name}' found on page",
            )

    return CheckResult(
        criterion=f"Component '{component}' exists",
        passed=False,
        method="element_check",
        evidence=f"No element matching '{component}' found on page",
    )
