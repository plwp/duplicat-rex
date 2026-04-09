"""
InteractiveExploreModule — deep interactive exploration via Playwright.

Goes beyond passive navigation (browser_explore) by clicking buttons,
filling forms, toggling toggles, and recording what each interaction does.
For each page it visits, it:
  1. Discovers all interactive elements (buttons, inputs, links, toggles)
  2. Exercises each safe element
  3. Records DOM state before/after and API calls triggered
  4. Produces structured Facts (UI_COMPONENT, API_ENDPOINT, USER_FLOW, BUSINESS_RULE)

Invariant compliance:
  INV-013: All facts have authority=AUTHORITATIVE
  INV-020: run() MUST NOT raise — all errors captured in ReconResult.errors
  INV-022: All facts have module_name="interactive_explore", source_type=LIVE_APP
  INV-028: Secrets never in facts or logs
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from scripts.models import (
    Authority,
    Confidence,
    EvidenceRef,
    Fact,
    FactCategory,
    RedactionStatus,
    SourceType,
)
from scripts.recon.base import (
    CoverageEntry,
    ReconError,
    ReconModule,
    ReconModuleStatus,
    ReconProgress,
    ReconRequest,
    ReconResult,
    ReconServices,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safety: deny-list of destructive button text patterns
# ---------------------------------------------------------------------------

_DESTRUCTIVE_PATTERNS: frozenset[str] = frozenset({
    "delete",
    "remove",
    "destroy",
    "permanently",
    "deactivate",
    "disable account",
    "close account",
    "cancel subscription",
    "unsubscribe",
    "send email",
    "send message",
    "send invoice",
    "pay now",
    "checkout",
    "purchase",
    "buy",
    "charge",
})

# Max interactive elements to exercise per page
_MAX_INTERACTIONS_PER_PAGE = 20


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class InteractiveElement:
    """A discovered interactive element on a page."""

    selector: str
    element_type: str  # "button", "link", "input", "toggle", "dropdown"
    text: str
    visible: bool
    input_type: str = ""  # for inputs: "text", "email", "password", "number", etc.
    placeholder: str = ""
    aria_label: str = ""


@dataclass
class InteractionResult:
    """The outcome of exercising one interactive element."""

    element: InteractiveElement
    url_changed: bool = False
    new_url: str | None = None
    dom_changed: bool = False
    modal_opened: bool = False
    api_calls: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    screenshot_path: str | None = None


# ---------------------------------------------------------------------------
# Module
# ---------------------------------------------------------------------------


class InteractiveExploreModule(ReconModule):
    """
    Active exploration module that clicks through the app to discover
    interactions, forms, modals, and state changes.

    Designed to run AFTER browser_explore has done passive navigation.
    Uses the same auth state file so it starts already authenticated.
    """

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #

    @property
    def name(self) -> str:
        return "interactive_explore"

    @property
    def authority(self) -> Authority:
        return Authority.AUTHORITATIVE

    @property
    def source_type(self) -> SourceType:
        return SourceType.LIVE_APP

    @property
    def requires_credentials(self) -> list[str]:
        return ["target.{domain}.username", "target.{domain}.password"]

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #

    async def run(
        self,
        request: ReconRequest,
        services: ReconServices,
        progress: Callable[[ReconProgress], None] | None = None,
    ) -> ReconResult:
        """
        Execute interactive exploration.

        INV-020: This method MUST NOT raise.
        """
        started_at = datetime.now(UTC).isoformat()
        t_start = time.monotonic()

        result = ReconResult(
            module=self.name,
            status=ReconModuleStatus.FAILED,
            started_at=started_at,
        )

        def emit(phase: str, message: str, **kw: Any) -> None:
            if progress:
                progress(
                    ReconProgress(
                        run_id=request.run_id,
                        module=self.name,
                        phase=phase,
                        message=message,
                        **kw,
                    )
                )

        try:
            emit("init", "Interactive explore module starting")

            browser = services.browser
            launched_locally = False
            if browser is None:
                browser, launched_locally = await self._launch_browser()

            try:
                auth_state_path = request.module_config.get("auth_state_path")
                if not auth_state_path and services.artifact_store:
                    candidate = Path(services.artifact_store).parent.parent / ".auth-state.json"
                    if candidate.exists():
                        auth_state_path = str(candidate)

                context_kwargs: dict[str, Any] = {
                    "viewport": {"width": 1280, "height": 800},
                }
                if auth_state_path and Path(auth_state_path).exists():
                    context_kwargs["storage_state"] = auth_state_path
                    emit("auth", f"Loading saved auth state from {Path(auth_state_path).name}")

                context = await browser.new_context(**context_kwargs)
                page = await context.new_page()

                # Determine pages to explore
                base_url = request.base_url or f"https://{request.target}"
                urls = request.module_config.get("urls_to_explore", [base_url])

                screenshot_dir = self._artifact_dir(services)
                all_facts: list[Fact] = []
                all_artifacts: dict[str, str] = {}
                all_errors: list[ReconError] = []
                coverage_map: dict[str, CoverageEntry] = {}
                features = request.scope.feature_keys() or ["general"]

                emit("discover", f"Exploring {len(urls)} pages interactively")

                for idx, url in enumerate(urls):
                    emit(
                        "discover",
                        f"Interacting with {url}",
                        completed=idx,
                        total=len(urls),
                    )
                    try:
                        page_facts, page_artifacts, page_errors = await self._explore_page(
                            page, url, request, services, screenshot_dir, features, emit
                        )
                        all_facts.extend(page_facts)
                        all_artifacts.update(page_artifacts)
                        all_errors.extend(page_errors)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("Error exploring page %s: %s", url, exc)
                        all_errors.append(
                            ReconError(
                                source_url=url,
                                error_type="parse_error",
                                message=f"Page exploration error: {type(exc).__name__}",
                                recoverable=True,
                            )
                        )

                    # Update coverage
                    feature = self._infer_feature(url, features)
                    entry = coverage_map.setdefault(feature, CoverageEntry(feature=feature))
                    entry.fact_count = sum(1 for f in all_facts if f.feature == feature)
                    entry.status = "observed" if entry.fact_count else "not_found"

                result.facts = all_facts
                result.artifacts = all_artifacts
                result.errors.extend(all_errors)
                result.coverage = list(coverage_map.values())
                result.urls_visited = list(urls)

                await context.close()

            finally:
                if launched_locally:
                    await browser.close()

            if result.facts:
                result.status = (
                    ReconModuleStatus.SUCCESS if not result.errors else ReconModuleStatus.PARTIAL
                )
            else:
                result.status = ReconModuleStatus.FAILED

            emit("complete", f"Done — {len(result.facts)} facts, {len(result.errors)} errors")

        except TimeoutError:
            result.errors.append(
                ReconError(
                    source_url=request.base_url or request.target,
                    error_type="timeout",
                    message="Interactive exploration timed out",
                    recoverable=True,
                )
            )
            result.status = ReconModuleStatus.FAILED

        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error in InteractiveExploreModule.run")
            result.errors.append(
                ReconError(
                    source_url=request.base_url or request.target,
                    error_type="parse_error",
                    message=f"Unexpected error: {type(exc).__name__}",
                    recoverable=False,
                )
            )
            result.status = ReconModuleStatus.FAILED

        result.finished_at = datetime.now(UTC).isoformat()
        result.duration_seconds = time.monotonic() - t_start
        return result

    async def validate_prerequisites(self) -> list[str]:
        """Check Playwright is importable."""
        missing = []
        try:
            import playwright  # noqa: F401
        except ImportError:
            missing.append("playwright (pip install playwright && playwright install)")
        return missing

    # ------------------------------------------------------------------ #
    # Page-level exploration
    # ------------------------------------------------------------------ #

    async def _explore_page(
        self,
        page: Any,
        url: str,
        request: ReconRequest,
        services: ReconServices,
        screenshot_dir: Path | None,
        features: list[str],
        emit: Callable[..., None],
    ) -> tuple[list[Fact], dict[str, str], list[ReconError]]:
        """Navigate to url, discover interactive elements, exercise each one."""
        facts: list[Fact] = []
        artifacts: dict[str, str] = {}
        errors: list[ReconError] = []
        feature = self._infer_feature(url, features)
        now = datetime.now(UTC).isoformat()

        try:
            await page.goto(url, timeout=30_000, wait_until="networkidle")
        except TimeoutError:
            pass  # Partial load is fine — still interact with what loaded
        except Exception as exc:  # noqa: BLE001
            return facts, artifacts, [
                ReconError(
                    source_url=url,
                    error_type="timeout",
                    message=f"Navigation error: {type(exc).__name__}",
                    recoverable=True,
                )
            ]

        # Wire up API call recorder for this page
        api_log: list[dict[str, Any]] = []
        _base = request.base_url or f"https://{request.target}"
        target_host = urlparse(_base).hostname or request.target
        self._attach_api_logger(page, api_log, target_host)

        # Discover all interactive elements
        elements = await self._discover_interactive_elements(page)
        emit("discover", f"Found {len(elements)} interactive elements on {url}")

        # Limit interactions per page
        elements_to_exercise = [e for e in elements if e.visible][: _MAX_INTERACTIONS_PER_PAGE]

        # Take initial screenshot
        initial_screenshot: str | None = None
        if screenshot_dir:
            initial_screenshot = await self._take_screenshot(
                page, url, screenshot_dir, suffix="before"
            )
            if initial_screenshot:
                artifacts[f"screenshot:before:{self._url_key(url)}"] = initial_screenshot

        for element in elements_to_exercise:
            if self._is_destructive(element):
                emit("discover", f"Skipping destructive element: '{element.text}'")
                continue

            result = await self._exercise_element(page, element, api_log, screenshot_dir, url)

            # Navigate back if URL changed (we don't want to leave the page)
            if result.url_changed and result.new_url:
                emit("discover", f"Navigation triggered by '{element.text}' → {result.new_url}")
                try:
                    await page.go_back(timeout=10_000)
                    await asyncio.sleep(0.5)
                except Exception:  # noqa: BLE001
                    # Can't go back — re-navigate
                    try:
                        await page.goto(url, timeout=15_000, wait_until="domcontentloaded")
                    except Exception:  # noqa: BLE001
                        pass

            # Dismiss any modal that appeared
            if result.modal_opened:
                await self._dismiss_modal(page)

            # Record screenshot artifact
            if result.screenshot_path:
                artifacts[f"screenshot:interaction:{self._url_key(url)}:{element.text[:20]}"] = (
                    result.screenshot_path
                )

            # Generate facts from this interaction
            new_facts = self._facts_from_result(result, url, feature, request.run_id, now)
            facts.extend(new_facts)

        return facts, artifacts, errors

    # ------------------------------------------------------------------ #
    # Element discovery
    # ------------------------------------------------------------------ #

    async def _discover_interactive_elements(self, page: Any) -> list[InteractiveElement]:
        """Find all clickable/interactive elements on the current page."""
        elements: list[InteractiveElement] = []

        # Buttons
        try:
            btn_handles = await page.query_selector_all(
                'button, [role="button"], input[type="submit"], input[type="button"]'
            )
            for btn in btn_handles:
                try:
                    text = (await btn.text_content() or "").strip()
                    aria = (await btn.get_attribute("aria-label") or "").strip()
                    visible = await btn.is_visible()
                    selector = await self._get_selector(btn, page)
                    elements.append(
                        InteractiveElement(
                            selector=selector,
                            element_type="button",
                            text=text or aria,
                            visible=visible,
                            aria_label=aria,
                        )
                    )
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

        # Internal links (navigation — low priority, skip nav-only)
        try:
            link_handles = await page.query_selector_all('a[href^="/"], a[href^="./"]')
            for link in link_handles:
                try:
                    text = (await link.text_content() or "").strip()
                    aria = (await link.get_attribute("aria-label") or "").strip()
                    visible = await link.is_visible()
                    selector = await self._get_selector(link, page)
                    elements.append(
                        InteractiveElement(
                            selector=selector,
                            element_type="link",
                            text=text or aria,
                            visible=visible,
                            aria_label=aria,
                        )
                    )
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

        # Form inputs
        try:
            _input_sel = (
                'input:not([type="submit"]):not([type="button"])'
                ':not([type="hidden"]), textarea, select'
            )
            input_handles = await page.query_selector_all(_input_sel)
            for inp in input_handles:
                try:
                    input_type = (await inp.get_attribute("type") or "text").strip()
                    placeholder = (await inp.get_attribute("placeholder") or "").strip()
                    aria = (await inp.get_attribute("aria-label") or "").strip()
                    name = (await inp.get_attribute("name") or "").strip()
                    visible = await inp.is_visible()
                    selector = await self._get_selector(inp, page)
                    elements.append(
                        InteractiveElement(
                            selector=selector,
                            element_type="input",
                            text=name or placeholder or aria,
                            visible=visible,
                            input_type=input_type,
                            placeholder=placeholder,
                            aria_label=aria,
                        )
                    )
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

        # Toggles and checkboxes
        try:
            toggle_handles = await page.query_selector_all(
                '[role="switch"], [role="checkbox"], [data-toggle], input[type="checkbox"]'
            )
            for toggle in toggle_handles:
                try:
                    text = (await toggle.text_content() or "").strip()
                    aria = (await toggle.get_attribute("aria-label") or "").strip()
                    visible = await toggle.is_visible()
                    selector = await self._get_selector(toggle, page)
                    elements.append(
                        InteractiveElement(
                            selector=selector,
                            element_type="toggle",
                            text=text or aria,
                            visible=visible,
                            aria_label=aria,
                        )
                    )
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

        # Dropdowns / selects
        try:
            dropdown_handles = await page.query_selector_all('[role="combobox"], [role="listbox"]')
            for dd in dropdown_handles:
                try:
                    text = (await dd.text_content() or "").strip()
                    aria = (await dd.get_attribute("aria-label") or "").strip()
                    visible = await dd.is_visible()
                    selector = await self._get_selector(dd, page)
                    elements.append(
                        InteractiveElement(
                            selector=selector,
                            element_type="dropdown",
                            text=text or aria,
                            visible=visible,
                            aria_label=aria,
                        )
                    )
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

        return elements

    # ------------------------------------------------------------------ #
    # Element exercise
    # ------------------------------------------------------------------ #

    async def _exercise_element(
        self,
        page: Any,
        element: InteractiveElement,
        api_log: list[dict[str, Any]],
        screenshot_dir: Path | None,
        page_url: str,
    ) -> InteractionResult:
        """Click/interact with an element and record what happens."""
        before_url = page.url
        before_dom = await self._dom_snapshot(page)
        api_log.clear()

        screenshot_path: str | None = None

        try:
            if element.element_type in ("button", "toggle"):
                await page.click(element.selector, timeout=5000)
            elif element.element_type == "input":
                if element.input_type == "checkbox":
                    await page.click(element.selector, timeout=5000)
                else:
                    test_val = self._test_value(element)
                    await page.fill(element.selector, test_val, timeout=5000)
            elif element.element_type == "dropdown":
                await page.click(element.selector, timeout=5000)
            elif element.element_type == "link":
                # Don't actually follow links — just record them
                return InteractionResult(element=element)
            else:
                await page.click(element.selector, timeout=5000)

            await asyncio.sleep(1)  # wait for side effects

            after_url = page.url
            after_dom = await self._dom_snapshot(page)
            modal = await self._detect_modal(page)

            # Screenshot after interaction
            if screenshot_dir:
                screenshot_path = await self._take_screenshot(
                    page, page_url, screenshot_dir, suffix=f"after_{element.text[:20]}"
                )

            return InteractionResult(
                element=element,
                url_changed=before_url != after_url,
                new_url=after_url if before_url != after_url else None,
                dom_changed=before_dom != after_dom,
                modal_opened=modal,
                api_calls=list(api_log),
                screenshot_path=screenshot_path,
            )

        except Exception as exc:  # noqa: BLE001
            return InteractionResult(element=element, error=str(exc))

    # ------------------------------------------------------------------ #
    # DOM helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    async def _dom_snapshot(page: Any) -> str:
        """Capture key structural elements for change detection."""
        try:
            return await page.evaluate(
                """() => {
                    const sel = 'h1, h2, h3, [role="dialog"], [role="alertdialog"],'
                        + ' form, .modal, .dialog';
                    const items = [...document.querySelectorAll(sel)].slice(0, 20);
                    return items.map(
                        el => el.tagName + ':' + (el.innerText || '').trim().slice(0, 50)
                    ).join('|');
                }"""
            )
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    async def _detect_modal(page: Any) -> bool:
        """Check if a modal/dialog appeared after interaction."""
        try:
            modal = await page.query_selector(
                '[role="dialog"], [role="alertdialog"], .modal, .dialog, [aria-modal="true"]'
            )
            if modal:
                return await modal.is_visible()
        except Exception:  # noqa: BLE001
            pass
        return False

    @staticmethod
    async def _dismiss_modal(page: Any) -> None:
        """Try to close any open modal."""
        try:
            # Try Escape key first
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
        except Exception:  # noqa: BLE001
            pass
        try:
            # Try common close button patterns
            _close_sel = (
                '[aria-label="Close"], [aria-label="close"], '
                'button.close, .modal-close, [data-dismiss="modal"]'
            )
            close_btn = await page.query_selector(_close_sel)
            if close_btn and await close_btn.is_visible():
                await close_btn.click(timeout=3000)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    async def _get_selector(element: Any, page: Any) -> str:
        """
        Generate a CSS selector for an element.
        Tries id → data-testid → aria-label → tag+text as fallback.
        """
        try:
            el_id = await element.get_attribute("id")
            if el_id:
                return f"#{el_id}"
        except Exception:  # noqa: BLE001
            pass
        try:
            test_id = await element.get_attribute("data-testid")
            if test_id:
                return f'[data-testid="{test_id}"]'
        except Exception:  # noqa: BLE001
            pass
        try:
            aria = await element.get_attribute("aria-label")
            if aria:
                return f'[aria-label="{aria}"]'
        except Exception:  # noqa: BLE001
            pass
        try:
            tag = await page.evaluate("el => el.tagName.toLowerCase()", element)
            text = (await element.text_content() or "").strip()[:30]
            if text:
                return f"{tag}:has-text(\"{text}\")"
            return tag
        except Exception:  # noqa: BLE001
            return "unknown"

    # ------------------------------------------------------------------ #
    # Test data generation
    # ------------------------------------------------------------------ #

    @staticmethod
    def _test_value(element: InteractiveElement) -> str:
        """Generate appropriate test data based on input type."""
        input_type = element.input_type.lower()
        placeholder = element.placeholder.lower()
        text = element.text.lower()

        if input_type == "email" or "email" in placeholder or "email" in text:
            return "test@example.com"
        if input_type == "password" or "password" in placeholder or "password" in text:
            return "TestPassword123!"
        if input_type == "number" or "count" in text or "quantity" in text:
            return "1"
        if input_type == "url" or "url" in placeholder or "website" in text:
            return "https://example.com"
        if input_type == "tel" or "phone" in placeholder or "phone" in text:
            return "+15550001234"
        if input_type == "date":
            return "2025-01-01"
        if "name" in placeholder or "name" in text:
            return "Test Name"
        if "search" in placeholder or "search" in text:
            return "test"
        if "description" in placeholder or "description" in text:
            return "Test description for exploration"
        # Generic fallback
        return "Test Value"

    # ------------------------------------------------------------------ #
    # Safety checks
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_destructive(element: InteractiveElement) -> bool:
        """Return True if this element looks destructive and should be skipped."""
        combined = (element.text + " " + element.aria_label).lower()
        return any(pattern in combined for pattern in _DESTRUCTIVE_PATTERNS)

    # ------------------------------------------------------------------ #
    # Fact generation
    # ------------------------------------------------------------------ #

    def _facts_from_result(
        self,
        result: InteractionResult,
        page_url: str,
        feature: str,
        run_id: str,
        now: str,
    ) -> list[Fact]:
        """Convert an InteractionResult into structured Facts."""
        facts: list[Fact] = []
        element = result.element

        if result.error:
            return facts  # No facts from failed interactions

        evidence = EvidenceRef(
            source_url=page_url,
            locator=element.selector,
            source_title=element.text or element.aria_label or None,
            artifact_uri=result.screenshot_path,
            captured_at=now,
        )

        # Modal fact
        if result.modal_opened:
            facts.append(
                Fact(
                    feature=feature,
                    category=FactCategory.UI_COMPONENT,
                    claim=(
                        f"Clicking '{element.text or element.aria_label}' on {page_url} "
                        f"opens a modal dialog"
                    ),
                    evidence=[evidence],
                    source_type=self.source_type,
                    module_name=self.name,
                    authority=self.authority,
                    confidence=Confidence.HIGH,
                    run_id=run_id,
                    structured_data={
                        "page_url": page_url,
                        "trigger_selector": element.selector,
                        "trigger_text": element.text,
                        "element_type": element.element_type,
                        "interaction": "modal_open",
                    },
                    observed_at=now,
                    redaction_status=RedactionStatus.CLEAN,
                )
            )

        # Navigation fact
        if result.url_changed and result.new_url:
            facts.append(
                Fact(
                    feature=feature,
                    category=FactCategory.USER_FLOW,
                    claim=(
                        f"Clicking '{element.text or element.aria_label}' on {page_url} "
                        f"navigates to {result.new_url}"
                    ),
                    evidence=[evidence],
                    source_type=self.source_type,
                    module_name=self.name,
                    authority=self.authority,
                    confidence=Confidence.HIGH,
                    run_id=run_id,
                    structured_data={
                        "page_url": page_url,
                        "trigger_selector": element.selector,
                        "trigger_text": element.text,
                        "destination_url": result.new_url,
                        "interaction": "navigation",
                    },
                    observed_at=now,
                    redaction_status=RedactionStatus.CLEAN,
                )
            )

        # API call facts
        for api_call in result.api_calls:
            method = api_call.get("method", "")
            path = api_call.get("path", "")
            status = api_call.get("status")

            if element.element_type == "input":
                # Form submission triggered an API call
                facts.append(
                    Fact(
                        feature=feature,
                        category=FactCategory.API_ENDPOINT,
                        claim=(
                            f"Submitting form via '{element.text or element.placeholder}' "
                            f"on {page_url} triggers {method} {path} "
                            f"(HTTP {status or 'unknown'})"
                        ),
                        evidence=[evidence],
                        source_type=self.source_type,
                        module_name=self.name,
                        authority=self.authority,
                        confidence=Confidence.HIGH,
                        run_id=run_id,
                        structured_data={
                            "method": method,
                            "path": path,
                            "status": status,
                            "trigger": "form_submit",
                            "form_field": element.text or element.placeholder,
                            "page_url": page_url,
                        },
                        observed_at=now,
                        redaction_status=RedactionStatus.CLEAN,
                    )
                )
                # User flow fact for form
                facts.append(
                    Fact(
                        feature=feature,
                        category=FactCategory.USER_FLOW,
                        claim=(
                            f"Form on {page_url} with field "
                            f"'{element.text or element.placeholder}' "
                            f"submits via {method} {path}"
                        ),
                        evidence=[evidence],
                        source_type=self.source_type,
                        module_name=self.name,
                        authority=self.authority,
                        confidence=Confidence.HIGH,
                        run_id=run_id,
                        structured_data={
                            "page_url": page_url,
                            "form_field": element.text or element.placeholder,
                            "input_type": element.input_type,
                            "api_method": method,
                            "api_path": path,
                        },
                        observed_at=now,
                        redaction_status=RedactionStatus.CLEAN,
                    )
                )
            else:
                # Button/toggle triggered an API call
                facts.append(
                    Fact(
                        feature=feature,
                        category=FactCategory.API_ENDPOINT,
                        claim=(
                            f"Clicking '{element.text or element.aria_label}' on {page_url} "
                            f"triggers {method} {path} (HTTP {status or 'unknown'})"
                        ),
                        evidence=[evidence],
                        source_type=self.source_type,
                        module_name=self.name,
                        authority=self.authority,
                        confidence=Confidence.HIGH,
                        run_id=run_id,
                        structured_data={
                            "method": method,
                            "path": path,
                            "status": status,
                            "trigger": "button_click",
                            "button_text": element.text,
                            "page_url": page_url,
                        },
                        observed_at=now,
                        redaction_status=RedactionStatus.CLEAN,
                    )
                )

        # Toggle state change fact
        if element.element_type == "toggle" and result.dom_changed and not result.api_calls:
            facts.append(
                Fact(
                    feature=feature,
                    category=FactCategory.BUSINESS_RULE,
                    claim=(
                        f"Toggling '{element.text or element.aria_label}' on {page_url} "
                        f"changes UI state (DOM modified without API call)"
                    ),
                    evidence=[evidence],
                    source_type=self.source_type,
                    module_name=self.name,
                    authority=self.authority,
                    confidence=Confidence.MEDIUM,
                    run_id=run_id,
                    structured_data={
                        "page_url": page_url,
                        "toggle_selector": element.selector,
                        "toggle_text": element.text,
                        "interaction": "toggle",
                        "dom_changed": True,
                        "api_calls_triggered": 0,
                    },
                    observed_at=now,
                    redaction_status=RedactionStatus.CLEAN,
                )
            )

        return facts

    # ------------------------------------------------------------------ #
    # Utility
    # ------------------------------------------------------------------ #

    @staticmethod
    def _infer_feature(url: str, features: list[str]) -> str:
        """Heuristically map a URL to the closest scope feature."""
        path = urlparse(url).path.lower()
        for feature in features:
            if feature.replace("-", "/") in path or feature in path:
                return feature
        return features[0] if features else "general"

    @staticmethod
    def _url_key(url: str) -> str:
        """Short stable key for a URL (for artifact naming)."""
        parsed = urlparse(url)
        path_slug = parsed.path.strip("/").replace("/", "_")[:30] or "home"
        return f"{(parsed.hostname or '').replace('.', '-')}_{path_slug}"

    @staticmethod
    def _artifact_dir(services: ReconServices) -> Path | None:
        """Resolve a writable directory for screenshots."""
        if services.artifact_store is None:
            return None
        try:
            store_path = Path(str(services.artifact_store))
            store_path.mkdir(parents=True, exist_ok=True)
            return store_path
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    async def _take_screenshot(
        page: Any, url: str, screenshot_dir: Path, suffix: str = ""
    ) -> str | None:
        """Take a screenshot and return its path, or None on failure."""
        try:
            parsed_url = urlparse(url)
            path_slug = parsed_url.path.strip("/").replace("/", "_")[:30] or "home"
            safe_name = f"{(parsed_url.hostname or '').replace('.', '-')}_{path_slug}"
            if suffix:
                safe_suffix = suffix.replace(" ", "_").replace("/", "-")[:30]
                safe_name = f"{safe_name}_{safe_suffix}"
            candidate = screenshot_dir / f"{safe_name}.png"
            counter = 2
            while candidate.exists():
                candidate = screenshot_dir / f"{safe_name}_{counter}.png"
                counter += 1
            await page.screenshot(path=str(candidate), full_page=False)
            return str(candidate)
        except Exception:  # noqa: BLE001
            return None

    def _attach_api_logger(
        self, page: Any, api_log: list[dict[str, Any]], target_host: str
    ) -> None:
        """Attach a response listener that logs product API calls into api_log."""
        from scripts.recon.browser_explore import BrowserExploreModule

        async def on_response(response: Any) -> None:
            try:
                req = response.request
                url = req.url
                if not BrowserExploreModule._is_product_request(url, target_host):
                    return
                parsed = urlparse(url)
                api_log.append({
                    "url": url,
                    "method": req.method,
                    "path": parsed.path,
                    "status": response.status,
                })
            except Exception:  # noqa: BLE001
                pass

        page.on("response", on_response)

    @staticmethod
    async def _launch_browser() -> tuple[Any, bool]:
        """Launch a local Playwright browser."""
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        return browser, True
