"""
API Documentation Scraper — ApiDocsModule.

Fetches and parses official API documentation pages to extract endpoint
definitions, request/response schemas, and auth requirements.

Produces one Fact per endpoint with category=API_ENDPOINT.

Supported doc formats:
  - HTML pages (BeautifulSoup parsing, follows pagination links)
  - OpenAPI / Swagger JSON or YAML specs (preferred when available)

INV-020: run() MUST NOT raise.
INV-013: All facts have authority=AUTHORITATIVE.
INV-001: Every Fact has at least one EvidenceRef.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from scripts.models import (
    Authority,
    Confidence,
    EvidenceRef,
    Fact,
    FactCategory,
    SourceType,
)
from scripts.recon.base import (
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
# Internal helpers
# ---------------------------------------------------------------------------

# HTTP headers that mimic a browser (reduces bot-detection false-positives)
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; duplicat-rex/0.1; +https://github.com/plwp/duplicat-rex)"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json,*/*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
}

# Back-off schedule for 429 responses (seconds)
_BACKOFF_SCHEDULE = [2, 5, 15, 30]

# Well-known OpenAPI spec paths to probe (relative to base URL)
_OPENAPI_PROBE_PATHS = [
    "/openapi.json",
    "/openapi.yaml",
    "/swagger.json",
    "/swagger.yaml",
    "/api-docs",
    "/api/openapi.json",
    "/v3/api-docs",
    "/api/v1/openapi.json",
    "/api/v2/openapi.json",
    "/api/v3/openapi.json",
]

# HTTP methods we recognise in HTML docs
_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}

# Max pages to follow when crawling HTML docs (safety valve)
_MAX_PAGES = 200


# ---------------------------------------------------------------------------
# Data containers (internal, not exported)
# ---------------------------------------------------------------------------


@dataclass
class _ParsedEndpoint:
    """Intermediate representation of a discovered API endpoint."""

    method: str
    path: str
    summary: str = ""
    description: str = ""
    parameters: list[dict[str, Any]] = field(default_factory=list)
    request_body: dict[str, Any] = field(default_factory=dict)
    responses: dict[str, Any] = field(default_factory=dict)
    auth_required: bool = False
    auth_schemes: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    source_url: str = ""
    raw_excerpt: str = ""


# ---------------------------------------------------------------------------
# Module
# ---------------------------------------------------------------------------


class ApiDocsModule(ReconModule):
    """
    Fetches and parses official API documentation.

    Strategy (in priority order):
      1. Probe for OpenAPI/Swagger spec — parse it directly (highest fidelity).
      2. Crawl HTML documentation pages — extract endpoints from prose/tables.

    For Trello specifically, we target developer.atlassian.com/cloud/trello/.
    """

    # --- ReconModule interface ---

    @property
    def name(self) -> str:
        return "api_docs"

    @property
    def authority(self) -> Authority:
        return Authority.AUTHORITATIVE

    @property
    def source_type(self) -> SourceType:
        return SourceType.API_DOCS

    @property
    def requires_credentials(self) -> list[str]:
        return []  # Public docs — no credentials needed

    # --- Main entry point ---

    async def run(
        self,
        request: ReconRequest,
        services: ReconServices,
        progress: Callable[[ReconProgress], None] | None = None,
    ) -> ReconResult:
        """
        Execute API doc recon.

        ENSURES: ReconResult.module == "api_docs".
        ENSURES: run() does not raise (INV-020).
        """
        started_at = datetime.now(UTC).isoformat()
        t0 = time.monotonic()

        def emit(
            phase: str, message: str, completed: int | None = None, total: int | None = None
        ) -> None:
            if progress:
                progress(
                    ReconProgress(
                        run_id=request.run_id,
                        module=self.name,
                        phase=phase,
                        message=message,
                        completed=completed,
                        total=total,
                    )
                )

        emit("init", f"Starting API docs recon for {request.target}")

        # Determine where to look
        base_url = request.base_url or f"https://{request.target}"
        target_slug = request.target.replace(".com", "").replace(".", "-")
        doc_url: str = request.module_config.get(
            "doc_url",
            f"https://developer.atlassian.com/cloud/{target_slug}/",
        )

        facts: list[Fact] = []
        errors: list[ReconError] = []
        urls_visited: list[str] = []

        try:
            # Build HTTP client — use injected client if available, else create one
            if services.http_client is not None:
                client = services.http_client
                own_client = False
            else:
                client = httpx.AsyncClient(
                    headers=_DEFAULT_HEADERS,
                    follow_redirects=True,
                    timeout=30.0,
                )
                own_client = True

            try:
                emit("discover", "Probing for OpenAPI/Swagger spec")
                endpoints, spec_url = await self._try_openapi(
                    client, base_url, request.module_config
                )

                if endpoints:
                    emit(
                        "extract",
                        f"Found OpenAPI spec at {spec_url}, parsing {len(endpoints)} endpoints",
                    )
                    source_hint = spec_url or base_url
                else:
                    emit("discover", f"No OpenAPI spec found, crawling HTML docs at {doc_url}")
                    endpoints, crawled_urls, crawl_errors = await self._crawl_html(
                        client, doc_url, request.budgets, emit
                    )
                    urls_visited.extend(crawled_urls)
                    errors.extend(crawl_errors)
                    source_hint = doc_url

                emit(
                    "extract",
                    f"Extracted {len(endpoints)} endpoints, building facts",
                    completed=len(endpoints),
                    total=len(endpoints),
                )

                scope_features = (
                    request.scope.feature_keys() if request.scope.resolved_features else []
                )

                for ep in endpoints:
                    fact = self._endpoint_to_fact(
                        ep,
                        request.run_id,
                        scope_features,
                        source_hint,
                    )
                    facts.append(fact)

            finally:
                if own_client:
                    await client.aclose()

        except Exception as exc:  # noqa: BLE001
            logger.exception("Unexpected error in ApiDocsModule.run")
            errors.append(
                ReconError(
                    source_url=None,
                    error_type="parse_error",
                    message=f"Unexpected error: {exc}",
                    recoverable=False,
                )
            )

        finished_at = datetime.now(UTC).isoformat()
        duration = time.monotonic() - t0

        if facts:
            status = ReconModuleStatus.PARTIAL if errors else ReconModuleStatus.SUCCESS
        else:
            status = ReconModuleStatus.FAILED

        emit("complete", f"Done: {len(facts)} facts, {len(errors)} errors")

        return ReconResult(
            module=self.name,
            status=status,
            facts=facts,
            errors=errors,
            urls_visited=urls_visited,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
            metrics={
                "endpoints_found": len(facts),
                "errors": len(errors),
                "urls_visited": len(urls_visited),
            },
        )

    # --- OpenAPI detection ---

    async def _try_openapi(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        module_config: dict[str, Any],
    ) -> tuple[list[_ParsedEndpoint], str]:
        """
        Probe well-known paths for an OpenAPI/Swagger spec.

        Returns (endpoints, spec_url) — empty list if no spec found.
        """
        # Allow caller to specify exact spec URL
        explicit_spec_url: str | None = module_config.get("openapi_spec_url")
        probe_paths = ([explicit_spec_url] if explicit_spec_url else []) + _OPENAPI_PROBE_PATHS

        for path in probe_paths:
            if path.startswith("http"):
                url = path
            else:
                url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
            try:
                resp = await client.get(url)
                if resp.status_code != 200:
                    continue

                content_type = resp.headers.get("content-type", "")
                text = resp.text.strip()

                is_yaml = (
                    "yaml" in content_type
                    or text.startswith("openapi:")
                    or text.startswith("swagger:")
                )
                if is_yaml:
                    endpoints = self._parse_openapi_yaml(text, url)
                    if endpoints:
                        return endpoints, url
                elif "json" in content_type or text.startswith("{"):
                    try:
                        spec = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    endpoints = self._parse_openapi_json(spec, url)
                    if endpoints:
                        return endpoints, url

            except (httpx.RequestError, httpx.TimeoutException):
                continue

        return [], ""

    def _parse_openapi_json(
        self, spec: dict[str, Any], spec_url: str
    ) -> list[_ParsedEndpoint]:
        """Parse an OpenAPI 3.x or Swagger 2.x JSON spec."""
        endpoints: list[_ParsedEndpoint] = []

        # Detect security schemes for auth info
        security_schemes: dict[str, str] = {}
        components = spec.get("components", spec.get("securityDefinitions", {}))
        if isinstance(components, dict):
            schemes = components.get("securitySchemes", components)
            if isinstance(schemes, dict):
                for scheme_name, scheme_def in schemes.items():
                    security_schemes[scheme_name] = scheme_def.get("type", "unknown")

        global_security: list[dict[str, Any]] = spec.get("security", [])

        paths: dict[str, Any] = spec.get("paths", {})
        for path, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue
            for method, operation in path_item.items():
                if method.upper() not in _HTTP_METHODS:
                    continue
                if not isinstance(operation, dict):
                    continue

                # Auth detection
                op_security = operation.get("security", global_security)
                auth_required = bool(op_security)
                auth_schemes = [list(s.keys())[0] for s in op_security if s]

                # Parameters
                parameters: list[dict[str, Any]] = []
                for param in operation.get("parameters", path_item.get("parameters", [])):
                    if isinstance(param, dict):
                        parameters.append({
                            "name": param.get("name", ""),
                            "in": param.get("in", ""),
                            "required": param.get("required", False),
                            "schema": param.get("schema", {}),
                            "description": param.get("description", ""),
                        })

                # Request body (OAS3)
                request_body: dict[str, Any] = {}
                rb = operation.get("requestBody", {})
                if rb:
                    content = rb.get("content", {})
                    for media_type, media_schema in content.items():
                        request_body[media_type] = media_schema.get("schema", {})

                # Responses
                responses: dict[str, Any] = {}
                for status_code, resp_def in operation.get("responses", {}).items():
                    if isinstance(resp_def, dict):
                        content = resp_def.get("content", {})
                        schema: dict[str, Any] = {}
                        for _, media in content.items():
                            schema = media.get("schema", {})
                            break
                        responses[str(status_code)] = {
                            "description": resp_def.get("description", ""),
                            "schema": schema,
                        }

                endpoints.append(
                    _ParsedEndpoint(
                        method=method.upper(),
                        path=path,
                        summary=operation.get("summary", ""),
                        description=operation.get("description", ""),
                        parameters=parameters,
                        request_body=request_body,
                        responses=responses,
                        auth_required=auth_required,
                        auth_schemes=auth_schemes,
                        tags=operation.get("tags", []),
                        source_url=spec_url,
                        raw_excerpt=f"{method.upper()} {path}",
                    )
                )

        return endpoints

    def _parse_openapi_yaml(self, text: str, spec_url: str) -> list[_ParsedEndpoint]:
        """Parse a YAML OpenAPI spec by converting to dict first."""
        try:
            import yaml  # type: ignore[import]

            spec = yaml.safe_load(text)
            if isinstance(spec, dict):
                return self._parse_openapi_json(spec, spec_url)
        except ImportError:
            logger.warning("PyYAML not installed — cannot parse YAML specs")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to parse YAML spec: %s", exc)
        return []

    # --- HTML crawling ---

    async def _crawl_html(
        self,
        client: httpx.AsyncClient,
        start_url: str,
        budgets: dict[str, int],
        emit: Callable[[str, str, int | None, int | None], None],
    ) -> tuple[list[_ParsedEndpoint], list[str], list[ReconError]]:
        """
        Crawl HTML documentation pages starting from start_url.

        Follows "next page" links within the same domain.
        Respects max_pages budget and rate-limits.
        """
        max_pages = budgets.get("max_pages", _MAX_PAGES)
        domain = urlparse(start_url).netloc

        visited: set[str] = set()
        queue: list[str] = [start_url]
        endpoints: list[_ParsedEndpoint] = []
        crawl_errors: list[ReconError] = []
        urls_visited: list[str] = []

        while queue and len(visited) < max_pages:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)

            total_est = min(len(visited) + len(queue), max_pages)
            emit("discover", f"Fetching {url}", len(visited), total_est)

            html, error = await self._fetch_with_backoff(client, url)
            if error:
                crawl_errors.append(error)
                continue

            urls_visited.append(url)
            soup = BeautifulSoup(html, "html.parser")

            # Extract endpoints from this page
            page_endpoints = self._extract_endpoints_from_html(soup, url)
            endpoints.extend(page_endpoints)

            # Find follow-up links (same domain, not yet visited)
            for link_tag in soup.find_all("a", href=True):
                href: str = link_tag["href"]
                abs_url = urljoin(url, href).split("#")[0]  # strip anchors
                parsed = urlparse(abs_url)
                if (
                    parsed.netloc == domain
                    and parsed.scheme in ("http", "https")
                    and abs_url not in visited
                    and abs_url not in queue
                ):
                    queue.append(abs_url)

        return endpoints, urls_visited, crawl_errors

    async def _fetch_with_backoff(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[str, ReconError | None]:
        """
        Fetch a URL with exponential backoff on 429.
        Returns (html_text, error_or_None).
        """
        for attempt, backoff in enumerate([0] + _BACKOFF_SCHEDULE):
            if backoff:
                await asyncio.sleep(backoff)
            try:
                resp = await client.get(url)
            except httpx.TimeoutException:
                return "", ReconError(
                    source_url=url,
                    error_type="timeout",
                    message=f"Request timed out: {url}",
                    recoverable=True,
                )
            except httpx.RequestError as exc:
                return "", ReconError(
                    source_url=url,
                    error_type="parse_error",
                    message=f"Network error fetching {url}: {exc}",
                    recoverable=False,
                )

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", backoff or 5))
                logger.warning("Rate limited on %s, retrying after %ds", url, retry_after)
                if attempt < len(_BACKOFF_SCHEDULE):
                    await asyncio.sleep(retry_after)
                    continue
                return "", ReconError(
                    source_url=url,
                    error_type="rate_limited",
                    message=f"Rate limited after {attempt + 1} attempts: {url}",
                    recoverable=True,
                )

            if resp.status_code >= 400:
                return "", ReconError(
                    source_url=url,
                    error_type="parse_error",
                    message=f"HTTP {resp.status_code} fetching {url}",
                    recoverable=resp.status_code >= 500,
                )

            return resp.text, None

        return "", ReconError(
            source_url=url,
            error_type="rate_limited",
            message=f"Exhausted retries for {url}",
            recoverable=True,
        )

    def _extract_endpoints_from_html(
        self, soup: BeautifulSoup, page_url: str
    ) -> list[_ParsedEndpoint]:
        """
        Extract API endpoints from an HTML documentation page.

        Heuristics:
          1. Code blocks containing METHOD /path patterns.
          2. Table rows with method + path columns.
          3. Heading + code-block combinations (common in Atlassian docs).
        """
        endpoints: list[_ParsedEndpoint] = []
        seen: set[tuple[str, str]] = set()

        page_title = soup.title.get_text(strip=True) if soup.title else page_url

        # --- Heuristic 1: code blocks with METHOD /path ---
        method_path_re = re.compile(
            r"\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+(/[\w\-/{}:.?&=%*]+)",
            re.IGNORECASE,
        )

        for code in soup.find_all(["code", "pre"]):
            text = code.get_text(" ", strip=True)
            for match in method_path_re.finditer(text):
                method = match.group(1).upper()
                path = match.group(2)
                key = (method, path)
                if key in seen:
                    continue
                seen.add(key)

                # Walk up to find surrounding description
                summary = self._find_nearby_heading(code)
                auth_required, auth_schemes = self._detect_auth_in_context(code)

                endpoints.append(
                    _ParsedEndpoint(
                        method=method,
                        path=path,
                        summary=summary,
                        source_url=page_url,
                        raw_excerpt=text[:500],
                        auth_required=auth_required,
                        auth_schemes=auth_schemes,
                        tags=[page_title[:80]],
                    )
                )

        # --- Heuristic 2: table rows ---
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            # Look for tables that have "method" and "path" (or "endpoint") columns
            method_col = next(
                (i for i, h in enumerate(headers) if "method" in h), None
            )
            path_col = next(
                (i for i, h in enumerate(headers) if "path" in h or "endpoint" in h or "url" in h),
                None,
            )
            if method_col is None or path_col is None:
                continue

            for row in table.find_all("tr")[1:]:  # skip header
                cells = row.find_all(["td", "th"])
                if len(cells) <= max(method_col, path_col):
                    continue
                raw_method = cells[method_col].get_text(strip=True).upper()
                path = cells[path_col].get_text(strip=True)
                if raw_method not in _HTTP_METHODS:
                    continue
                key = (raw_method, path)
                if key in seen:
                    continue
                seen.add(key)

                # Grab description from adjacent column if available
                desc_col = next(
                    (i for i, h in enumerate(headers) if "desc" in h or "summary" in h),
                    None,
                )
                summary = (
                    cells[desc_col].get_text(strip=True)
                    if desc_col and len(cells) > desc_col
                    else ""
                )

                endpoints.append(
                    _ParsedEndpoint(
                        method=raw_method,
                        path=path,
                        summary=summary,
                        source_url=page_url,
                        raw_excerpt=row.get_text(" ", strip=True)[:500],
                        tags=[page_title[:80]],
                    )
                )

        return endpoints

    def _find_nearby_heading(self, element: Any) -> str:
        """Walk up the DOM tree to find the nearest preceding heading."""
        for parent in element.parents:
            if parent is None:
                break
            prev = parent.find_previous_sibling(["h1", "h2", "h3", "h4", "h5", "h6"])
            if prev:
                return prev.get_text(strip=True)[:200]
        return ""

    def _detect_auth_in_context(self, element: Any) -> tuple[bool, list[str]]:
        """Heuristically detect auth requirement near an endpoint element."""
        # Look at surrounding text for auth keywords
        context_text = ""
        for parent in element.parents:
            if parent is None:
                break
            context_text = parent.get_text(" ", strip=True)[:1000]
            break

        _auth_re = r"\b(auth|token|key|oauth|bearer|api.?key|authentication)\b"
        auth_required = bool(re.search(_auth_re, context_text, re.I))
        auth_schemes: list[str] = []
        if re.search(r"\boauth\b", context_text, re.I):
            auth_schemes.append("oauth2")
        if re.search(r"\bbearer\b", context_text, re.I):
            auth_schemes.append("bearerAuth")
        if re.search(r"\bapi.?key\b", context_text, re.I):
            auth_schemes.append("apiKey")

        return auth_required, auth_schemes

    # --- Fact creation ---

    def _endpoint_to_fact(
        self,
        ep: _ParsedEndpoint,
        run_id: str,
        scope_features: list[str],
        source_hint: str,
    ) -> Fact:
        """Convert a _ParsedEndpoint to a Fact."""
        # Derive feature from path segments or tags
        feature = self._infer_feature(ep, scope_features)

        # Build claim
        claim_parts = [f"The API exposes {ep.method} {ep.path}"]
        if ep.summary:
            claim_parts.append(f"({ep.summary})")
        if ep.auth_required:
            schemes_str = ", ".join(ep.auth_schemes) if ep.auth_schemes else "unspecified scheme"
            claim_parts.append(f"requiring authentication via {schemes_str}")
        else:
            claim_parts.append("with no authentication required")
        claim = " ".join(claim_parts) + "."

        evidence = EvidenceRef(
            source_url=ep.source_url or source_hint,
            locator=f"{ep.method} {ep.path}",
            source_title=ep.tags[0] if ep.tags else None,
            raw_excerpt=ep.raw_excerpt[:2000] if ep.raw_excerpt else None,
        )

        structured_data: dict[str, Any] = {
            "method": ep.method,
            "path": ep.path,
            "summary": ep.summary,
            "description": ep.description,
            "parameters": ep.parameters,
            "request_body": ep.request_body,
            "responses": ep.responses,
            "auth_required": ep.auth_required,
            "auth_schemes": ep.auth_schemes,
            "tags": ep.tags,
        }

        return Fact(
            feature=feature,
            category=FactCategory.API_ENDPOINT,
            claim=claim,
            evidence=[evidence],
            source_type=self.source_type,
            structured_data=structured_data,
            module_name=self.name,
            authority=self.authority,
            confidence=Confidence.HIGH,
            run_id=run_id,
        )

    def _infer_feature(self, ep: _ParsedEndpoint, scope_features: list[str]) -> str:
        """
        Infer the feature key from the endpoint path and any scope features.

        Strategy:
          1. Match path segments against known scope features.
          2. Fall back to the first meaningful path segment.
          3. Default to "api-endpoints".
        """
        path_segments = [s for s in ep.path.strip("/").split("/") if s and not s.startswith("{")]

        # Try to match a scope feature against path segments
        for feature in scope_features:
            feature_slug = feature.lower().replace("-", "").replace("_", "")
            for seg in path_segments:
                if feature_slug in seg.lower().replace("-", "").replace("_", ""):
                    return feature

        # Use the first non-version path segment
        for seg in path_segments:
            # Skip version prefixes like "v1", "v2", "api", "1"
            if re.match(r"^(v\d+|\d+|api)$", seg, re.I):
                continue
            return re.sub(r"[^a-z0-9-]", "-", seg.lower()).strip("-") or "api-endpoints"

        return "api-endpoints"
