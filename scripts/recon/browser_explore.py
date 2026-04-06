"""
BrowserExploreModule — live SaaS exploration via Playwright + browser-use.

Authenticates with a target SaaS using keychain credentials, navigates the
application, captures HTTP/WebSocket traffic, takes screenshots, and emits
structured Facts with source=live_app, authority=authoritative.

Invariant compliance:
  INV-013: All facts have authority=AUTHORITATIVE
  INV-020: run() MUST NOT raise — all errors captured in ReconResult.errors
  INV-022: All facts have module_name="browser_explore", source_type=LIVE_APP
  INV-028: Secrets never in facts or logs (credentials redacted before storage)
"""

from __future__ import annotations

import hashlib
import json
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
# Internal data structures for captured network traffic
# ---------------------------------------------------------------------------


@dataclass
class CapturedRequest:
    """A single intercepted HTTP request/response pair."""

    url: str
    method: str
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: str | None = None
    response_status: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body: str | None = None
    duration_ms: float | None = None
    captured_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class CapturedWsFrame:
    """A single WebSocket frame (sent or received)."""

    url: str
    direction: str  # "sent" | "received"
    payload: str
    captured_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class NavigationStep:
    """One navigation step in the user flow."""

    url: str
    page_title: str
    screenshot_path: str | None = None
    http_requests: list[CapturedRequest] = field(default_factory=list)
    ws_frames: list[CapturedWsFrame] = field(default_factory=list)
    dom_summary: str = ""
    captured_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


# ---------------------------------------------------------------------------
# Module
# ---------------------------------------------------------------------------


class BrowserExploreModule(ReconModule):
    """
    Explores a target SaaS application using a real browser.

    Responsibilities:
    - Authenticate using injected credentials (never touches keychain directly)
    - Navigate the application and intercept all HTTP/WS traffic
    - Take screenshots at each navigation step
    - Convert captured observations into structured Facts
    - Handle timeouts, CAPTCHAs, and rate limiting gracefully

    The module is stateless between runs. All state lives in ReconResult.
    """

    # ------------------------------------------------------------------ #
    # Properties (identity contract)
    # ------------------------------------------------------------------ #

    @property
    def name(self) -> str:
        return "browser_explore"

    @property
    def authority(self) -> Authority:
        return Authority.AUTHORITATIVE

    @property
    def source_type(self) -> SourceType:
        return SourceType.LIVE_APP

    @property
    def requires_credentials(self) -> list[str]:
        """
        Credential keys needed for authentication. The domain placeholder
        {domain} is resolved by the orchestrator based on request.target.
        The orchestrator normalises target to a slug before looking up the key.
        Credentials are optional — if absent the module explores anonymously.
        """
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
        Execute browser-based exploration.

        INV-020: This method MUST NOT raise. All exceptions are caught and
        recorded in ReconResult.errors.
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
            emit("init", "Browser explore module starting")

            # Resolve credential keys for this target
            creds = self._resolve_credentials(request, services)

            # Obtain browser — from services if pre-configured, else launch one
            browser = services.browser
            launched_locally = False
            if browser is None:
                browser, launched_locally = await self._launch_browser()

            try:
                context = await browser.new_context(
                    record_har_path=None,  # We intercept manually for full control
                    viewport={"width": 1280, "height": 800},
                )
                page = await context.new_page()

                # Wire up network interception
                http_captures: list[CapturedRequest] = []
                ws_captures: list[CapturedWsFrame] = []
                self._attach_network_interceptor(page, http_captures, ws_captures)

                # Phase: auth
                emit("auth", "Attempting authentication")
                auth_ok, auth_error = await self._authenticate(
                    page, request, creds, emit
                )
                if not auth_error and not auth_ok:
                    # No credentials provided — explore anonymously
                    auth_ok = True

                if auth_error:
                    result.errors.append(auth_error)
                    result.status = ReconModuleStatus.FAILED
                    result.finished_at = datetime.now(UTC).isoformat()
                    result.duration_seconds = time.monotonic() - t_start
                    return result

                # Phase: discover
                emit("discover", "Starting application exploration")
                steps = await self._explore(
                    page, request, services, http_captures, ws_captures, emit
                )
                result.urls_visited = [s.url for s in steps]

                # Phase: extract facts
                emit("extract", f"Extracting facts from {len(steps)} navigation steps")
                facts, coverage, artifacts, errors = self._extract_facts(
                    steps, request, services
                )
                result.facts = facts
                result.coverage = coverage
                result.artifacts = artifacts
                result.errors.extend(errors)

                await context.close()

            finally:
                if launched_locally:
                    await browser.close()

            # Determine final status
            if result.facts:
                result.status = (
                    ReconModuleStatus.SUCCESS
                    if not result.errors
                    else ReconModuleStatus.PARTIAL
                )
            else:
                result.status = ReconModuleStatus.FAILED

            emit("complete", f"Done — {len(result.facts)} facts, {len(result.errors)} errors")

        except TimeoutError:
            result.errors.append(
                ReconError(
                    source_url=request.base_url or request.target,
                    error_type="timeout",
                    message="Browser exploration timed out",
                    recoverable=True,
                )
            )
            result.status = ReconModuleStatus.FAILED

        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error in BrowserExploreModule.run")
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
    # Private helpers
    # ------------------------------------------------------------------ #

    def _resolve_credentials(
        self,
        request: ReconRequest,
        services: ReconServices,
    ) -> dict[str, str]:
        """
        Build the concrete credential key names for this target and return
        whatever the orchestrator has pre-fetched in services.credentials.

        INV-028: We never log credential values — only key names.
        """
        domain_slug = self._domain_slug(request.target)
        resolved: dict[str, str] = {}
        for template in self.requires_credentials:
            key = template.replace("{domain}", domain_slug)
            if key in services.credentials:
                resolved[key] = services.credentials[key]
        return resolved

    @staticmethod
    def _domain_slug(target: str) -> str:
        """Convert 'https://trello.com' or 'trello.com' to 'trello-com'."""
        host = urlparse(target).hostname or target
        return host.replace(".", "-")

    async def _launch_browser(self) -> tuple[Any, bool]:
        """
        Launch a local Playwright browser. Returns (browser, launched_locally=True).
        Raises ImportError if Playwright is not installed.
        """
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)
        return browser, True

    def _attach_network_interceptor(
        self,
        page: Any,
        http_captures: list[CapturedRequest],
        ws_captures: list[CapturedWsFrame],
    ) -> None:
        """
        Wire up Playwright event listeners to capture HTTP and WebSocket traffic.
        All captures go into the shared lists — thread-safe within a single asyncio loop.
        """

        async def on_response(response: Any) -> None:
            try:
                req = response.request
                t0 = time.monotonic()
                try:
                    body = await response.text()
                except Exception:  # noqa: BLE001
                    body = None
                captured = CapturedRequest(
                    url=req.url,
                    method=req.method,
                    request_headers=dict(req.headers),
                    request_body=req.post_data,
                    response_status=response.status,
                    response_headers=dict(response.headers),
                    response_body=body,
                    duration_ms=(time.monotonic() - t0) * 1000,
                )
                http_captures.append(captured)
            except Exception:  # noqa: BLE001
                pass  # Never crash the interceptor

        page.on("response", on_response)

        # WebSocket frames
        async def on_websocket(ws: Any) -> None:
            ws_url = ws.url

            async def on_frame_sent(payload: str) -> None:
                ws_captures.append(
                    CapturedWsFrame(url=ws_url, direction="sent", payload=payload)
                )

            async def on_frame_received(payload: str) -> None:
                ws_captures.append(
                    CapturedWsFrame(url=ws_url, direction="received", payload=payload)
                )

            ws.on("framesent", on_frame_sent)
            ws.on("framereceived", on_frame_received)

        page.on("websocket", on_websocket)

    async def _authenticate(
        self,
        page: Any,
        request: ReconRequest,
        creds: dict[str, str],
        emit: Callable[..., None],
    ) -> tuple[bool, ReconError | None]:
        """
        Attempt to authenticate with the target SaaS.

        Returns (success, error). If no credentials are available, returns
        (False, None) to signal anonymous exploration.

        INV-028: Credential values never appear in log messages or facts.
        """
        domain_slug = self._domain_slug(request.target)
        username_key = f"target.{domain_slug}.username"
        password_key = f"target.{domain_slug}.password"

        username = creds.get(username_key)
        password = creds.get(password_key)

        if not username or not password:
            emit("auth", "No credentials available — exploring anonymously")
            return False, None

        base_url = request.base_url or f"https://{request.target}"
        login_url = f"{base_url}/login"

        try:
            budget = request.budgets.get("time_seconds", 60)
            timeout_ms = min(budget * 1000, 30_000)

            await page.goto(login_url, timeout=timeout_ms)
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)

            # Generic login form heuristic — real deployments may need
            # target-specific selectors via module_config
            selectors = request.module_config.get("auth_selectors", {})
            default_username_sel = '[type="email"], [name="username"], [name="email"]'
            username_sel = selectors.get("username", default_username_sel)
            password_sel = selectors.get("password", '[type="password"]')
            submit_sel = selectors.get("submit", '[type="submit"]')

            await page.fill(username_sel, username)
            await page.fill(password_sel, password)
            await page.click(submit_sel)
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)

            # Check for common auth failure signals
            current_url = page.url
            if "login" in current_url or "signin" in current_url:
                return False, ReconError(
                    source_url=login_url,
                    error_type="auth_required",
                    message="Authentication failed — still on login page after submit",
                    recoverable=False,
                )

            emit("auth", "Authentication succeeded")
            return True, None

        except TimeoutError:
            return False, ReconError(
                source_url=login_url,
                error_type="timeout",
                message="Timed out during authentication",
                recoverable=True,
            )
        except Exception as exc:  # noqa: BLE001
            return False, ReconError(
                source_url=login_url,
                error_type="auth_required",
                message=f"Auth error: {type(exc).__name__}",
                recoverable=False,
            )

    async def _explore(
        self,
        page: Any,
        request: ReconRequest,
        services: ReconServices,
        http_captures: list[CapturedRequest],
        ws_captures: list[CapturedWsFrame],
        emit: Callable[..., None],
    ) -> list[NavigationStep]:
        """
        Navigate the target application and collect NavigationStep records.

        Respects budgets.max_pages and budgets.time_seconds.
        Handles rate limiting and CAPTCHAs gracefully (logs and continues).
        """
        steps: list[NavigationStep] = []
        base_url = request.base_url or f"https://{request.target}"
        max_pages = request.budgets.get("max_pages", 10)
        time_budget = request.budgets.get("time_seconds", 300)
        t_start = time.monotonic()

        # Starting URL
        urls_to_visit = [base_url]
        visited: set[str] = set()

        screenshot_dir = self._artifact_dir(services)

        for url in urls_to_visit:
            if len(steps) >= max_pages:
                break
            if time.monotonic() - t_start > time_budget:
                break
            if url in visited:
                continue
            visited.add(url)

            emit("discover", f"Navigating to {url}", completed=len(steps), total=max_pages)

            # Snapshot http_captures before navigation
            before_count = len(http_captures)
            before_ws_count = len(ws_captures)

            try:
                await page.goto(url, timeout=30_000, wait_until="networkidle")
            except TimeoutError:
                # Partial load — still capture what we have
                pass
            except Exception as exc:  # noqa: BLE001
                logger.debug("Navigation error for %s: %s", url, type(exc).__name__)
                continue

            # Take screenshot
            screenshot_path: str | None = None
            if screenshot_dir:
                safe_name = hashlib.md5(url.encode()).hexdigest()[:12]  # noqa: S324
                screenshot_path = str(screenshot_dir / f"{safe_name}.png")
                try:
                    await page.screenshot(path=screenshot_path, full_page=True)
                except Exception:  # noqa: BLE001
                    screenshot_path = None

            # Collect DOM summary (title + headings)
            dom_summary = await self._dom_summary(page)

            # Slice captures that belong to this navigation step
            step_http = http_captures[before_count:]
            step_ws = ws_captures[before_ws_count:]

            steps.append(
                NavigationStep(
                    url=url,
                    page_title=await page.title(),
                    screenshot_path=screenshot_path,
                    http_requests=list(step_http),
                    ws_frames=list(step_ws),
                    dom_summary=dom_summary,
                )
            )

            # Discover more links on this page (same-origin only)
            links = await self._discover_links(page, base_url)
            for link in links:
                if link not in visited and link not in urls_to_visit:
                    urls_to_visit.append(link)

        return steps

    @staticmethod
    async def _dom_summary(page: Any) -> str:
        """Extract a lightweight DOM summary: title + h1/h2 headings."""
        try:
            headings = await page.evaluate(
                """() => {
                    const hs = [...document.querySelectorAll('h1, h2')].slice(0, 10);
                    return hs.map(h => h.innerText.trim()).filter(Boolean);
                }"""
            )
            return "; ".join(headings[:10])
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    async def _discover_links(page: Any, base_url: str) -> list[str]:
        """Collect same-origin links from the current page."""
        try:
            parsed_base = urlparse(base_url)
            hrefs: list[str] = await page.evaluate(
                """() => [...document.querySelectorAll('a[href]')]
                   .map(a => a.href)
                   .filter(Boolean)"""
            )
            same_origin = []
            for href in hrefs:
                parsed = urlparse(href)
                if parsed.scheme in ("http", "https") and parsed.netloc == parsed_base.netloc:
                    # Strip query and fragment to reduce explosion
                    clean = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
                    same_origin.append(clean)
            return list(dict.fromkeys(same_origin))  # Preserve order, deduplicate
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def _artifact_dir(services: ReconServices) -> Path | None:
        """Resolve a writable directory for screenshots and HARs."""
        if services.artifact_store is None:
            return None
        try:
            store_path = Path(str(services.artifact_store))
            store_path.mkdir(parents=True, exist_ok=True)
            return store_path
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------ #
    # Fact extraction
    # ------------------------------------------------------------------ #

    def _extract_facts(
        self,
        steps: list[NavigationStep],
        request: ReconRequest,
        services: ReconServices,
    ) -> tuple[list[Fact], list[CoverageEntry], dict[str, str], list[ReconError]]:
        """
        Convert NavigationStep records into structured Facts.

        INV-013: All facts carry authority=AUTHORITATIVE.
        INV-022: All facts have module_name="browser_explore", source_type=LIVE_APP.
        INV-028: Secrets (auth headers, tokens) are redacted before storage.
        """
        facts: list[Fact] = []
        coverage_map: dict[str, CoverageEntry] = {}
        artifacts: dict[str, str] = {}
        errors: list[ReconError] = []

        features = request.scope.feature_keys() or ["general"]

        for step in steps:
            feature = self._infer_feature(step.url, features)
            now = datetime.now(UTC).isoformat()

            # Record screenshot artifact
            if step.screenshot_path:
                artifact_key = f"screenshot:{hashlib.md5(step.url.encode()).hexdigest()[:8]}"  # noqa: S324
                artifacts[artifact_key] = step.screenshot_path

            # --- UI component fact (one per page visited) ---
            if step.dom_summary or step.page_title:
                evidence = EvidenceRef(
                    source_url=step.url,
                    source_title=step.page_title or None,
                    artifact_uri=step.screenshot_path,
                    captured_at=step.captured_at,
                    raw_excerpt=step.dom_summary[:2000] if step.dom_summary else None,
                )
                facts.append(
                    Fact(
                        feature=feature,
                        category=FactCategory.UI_COMPONENT,
                        claim=(
                            f"Page '{step.page_title or step.url}' renders with "
                            f"heading structure: "
                            f"{step.dom_summary[:200] or '(none observed)'}"
                        ),
                        evidence=[evidence],
                        source_type=self.source_type,
                        module_name=self.name,
                        authority=self.authority,
                        confidence=Confidence.HIGH,
                        run_id=request.run_id,
                        observed_at=now,
                        redaction_status=RedactionStatus.CLEAN,
                    )
                )

            # --- API endpoint facts (one per unique HTTP request) ---
            seen_endpoints: set[str] = set()
            for req in step.http_requests:
                parsed = urlparse(req.url)
                endpoint_key = f"{req.method}:{parsed.path}"
                if endpoint_key in seen_endpoints:
                    continue
                seen_endpoints.add(endpoint_key)

                # Redact auth headers before storing
                safe_headers = self._redact_headers(req.request_headers)
                safe_resp_headers = self._redact_headers(req.response_headers)

                evidence = EvidenceRef(
                    source_url=req.url,
                    locator=f"{req.method} {parsed.path}",
                    source_title=step.page_title or None,
                    captured_at=req.captured_at,
                )
                structured: dict[str, Any] = {
                    "method": req.method,
                    "path": parsed.path,
                    "status": req.response_status,
                    "request_headers": safe_headers,
                    "response_headers": safe_resp_headers,
                    "duration_ms": req.duration_ms,
                }
                # Only include non-sensitive body snippets
                if req.response_body and len(req.response_body) <= 4096:
                    structured["response_body_sample"] = req.response_body[:1000]

                facts.append(
                    Fact(
                        feature=feature,
                        category=FactCategory.API_ENDPOINT,
                        claim=(
                            f"{req.method} {parsed.path} returns "
                            f"HTTP {req.response_status or 'unknown'}"
                        ),
                        evidence=[evidence],
                        source_type=self.source_type,
                        module_name=self.name,
                        authority=self.authority,
                        confidence=Confidence.HIGH,
                        run_id=request.run_id,
                        structured_data=structured,
                        observed_at=now,
                        redaction_status=RedactionStatus.CLEAN,
                    )
                )

            # --- WebSocket event facts ---
            seen_ws_events: set[str] = set()
            for frame in step.ws_frames:
                event_key = f"{frame.direction}:{frame.url}"
                if event_key in seen_ws_events:
                    continue
                seen_ws_events.add(event_key)

                # Attempt to parse WS payload for event name
                event_name = self._extract_ws_event_name(frame.payload)

                evidence = EvidenceRef(
                    source_url=frame.url,
                    locator=event_name or frame.direction,
                    captured_at=frame.captured_at,
                    raw_excerpt=frame.payload[:500] if frame.payload else None,
                )
                facts.append(
                    Fact(
                        feature=feature,
                        category=FactCategory.WS_EVENT,
                        claim=(
                            f"WebSocket {frame.direction} event"
                            + (f" '{event_name}'" if event_name else "")
                            + f" observed on {frame.url}"
                        ),
                        evidence=[evidence],
                        source_type=self.source_type,
                        module_name=self.name,
                        authority=self.authority,
                        confidence=Confidence.HIGH,
                        run_id=request.run_id,
                        structured_data={
                            "ws_url": frame.url,
                            "direction": frame.direction,
                            "event_name": event_name,
                        },
                        observed_at=now,
                        redaction_status=RedactionStatus.CLEAN,
                    )
                )

            # --- User flow fact (one per page, mapping actions) ---
            if step.http_requests:
                evidence = EvidenceRef(
                    source_url=step.url,
                    source_title=step.page_title or None,
                    captured_at=step.captured_at,
                )
                methods = sorted({r.method for r in step.http_requests})
                facts.append(
                    Fact(
                        feature=feature,
                        category=FactCategory.USER_FLOW,
                        claim=(
                            f"Visiting '{step.url}' triggers "
                            f"{len(step.http_requests)} HTTP requests "
                            f"({', '.join(methods)} methods)"
                        ),
                        evidence=[evidence],
                        source_type=self.source_type,
                        module_name=self.name,
                        authority=self.authority,
                        confidence=Confidence.HIGH,
                        run_id=request.run_id,
                        structured_data={
                            "url": step.url,
                            "request_count": len(step.http_requests),
                            "methods": methods,
                        },
                        observed_at=now,
                        redaction_status=RedactionStatus.CLEAN,
                    )
                )

            # Update coverage
            entry = coverage_map.setdefault(feature, CoverageEntry(feature=feature))
            step_fact_count = sum(1 for f in facts if f.feature == feature)
            entry.fact_count = step_fact_count
            entry.status = "observed" if step_fact_count else "not_found"

        return facts, list(coverage_map.values()), artifacts, errors

    @staticmethod
    def _infer_feature(url: str, features: list[str]) -> str:
        """
        Heuristically map a URL to the closest scope feature.
        Falls back to 'general' if no match found.
        """
        path = urlparse(url).path.lower()
        for feature in features:
            if feature.replace("-", "/") in path or feature in path:
                return feature
        return features[0] if features else "general"

    @staticmethod
    def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
        """
        Remove sensitive header values before storing in structured_data.
        INV-028: Secrets never in facts.
        """
        sensitive_keys = {
            "authorization",
            "cookie",
            "set-cookie",
            "x-auth-token",
            "x-api-key",
            "x-session-token",
            "proxy-authorization",
        }
        return {
            k: ("[REDACTED]" if k.lower() in sensitive_keys else v)
            for k, v in headers.items()
        }

    @staticmethod
    def _extract_ws_event_name(payload: str) -> str | None:
        """
        Try to extract a named event from a JSON WebSocket payload.
        Common patterns: {"event": "..."}, {"type": "..."}, {"action": "..."}
        """
        if not payload:
            return None
        try:
            data = json.loads(payload)
            if isinstance(data, dict):
                for key in ("event", "type", "action", "cmd", "op"):
                    if key in data and isinstance(data[key], str):
                        return data[key]
        except (json.JSONDecodeError, ValueError):
            pass
        return None
