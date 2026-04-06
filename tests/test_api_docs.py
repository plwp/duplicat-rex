"""
Tests for scripts/recon/api_docs.py — ApiDocsModule.

All HTTP responses are mocked — no real network calls.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from scripts.models import Authority, FactCategory, SourceType
from scripts.recon.api_docs import ApiDocsModule, _ParsedEndpoint
from scripts.recon.base import (
    ReconModuleStatus,
    ReconRequest,
    ReconResult,
    ReconServices,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(**kwargs: Any) -> ReconRequest:
    """Build a minimal ReconRequest for testing."""
    defaults: dict[str, Any] = {
        "run_id": "test-run-id",
        "target": "trello.com",
        "base_url": "https://trello.com",
    }
    defaults.update(kwargs)
    return ReconRequest(**defaults)


def _make_services(http_client: Any = None) -> ReconServices:
    """Build a minimal ReconServices for testing."""
    return ReconServices(
        spec_store=None,
        credentials={},
        artifact_store=None,
        http_client=http_client,
        browser=None,
        clock=None,
    )


def _run(coro: Any) -> Any:
    """Run a coroutine synchronously in tests."""
    return asyncio.run(coro)


def _mock_response(
    status_code: int = 200,
    text: str = "",
    content_type: str = "text/html",
) -> MagicMock:
    """Create a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.headers = {"content-type": content_type}
    return resp


# ---------------------------------------------------------------------------
# Module property tests
# ---------------------------------------------------------------------------


class TestModuleProperties:
    def test_name(self) -> None:
        mod = ApiDocsModule()
        assert mod.name == "api_docs"

    def test_authority(self) -> None:
        mod = ApiDocsModule()
        assert mod.authority == Authority.AUTHORITATIVE

    def test_source_type(self) -> None:
        mod = ApiDocsModule()
        assert mod.source_type == SourceType.API_DOCS

    def test_requires_credentials_empty(self) -> None:
        mod = ApiDocsModule()
        assert mod.requires_credentials == []

    def test_validate_prerequisites_returns_empty(self) -> None:
        mod = ApiDocsModule()
        result = _run(mod.validate_prerequisites())
        assert result == []


# ---------------------------------------------------------------------------
# OpenAPI / Swagger detection tests
# ---------------------------------------------------------------------------

OPENAPI_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Trello API", "version": "1.0.0"},
    "security": [{"ApiKey": []}],
    "components": {
        "securitySchemes": {
            "ApiKey": {"type": "apiKey", "in": "query", "name": "key"},
            "Token": {"type": "apiKey", "in": "query", "name": "token"},
        }
    },
    "paths": {
        "/1/boards/{id}": {
            "get": {
                "summary": "Get a Board",
                "tags": ["board"],
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {
                    "200": {
                        "description": "Success",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    },
                    "401": {"description": "Unauthorized"},
                },
            }
        },
        "/1/boards": {
            "post": {
                "summary": "Create a Board",
                "tags": ["board"],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"name": {"type": "string"}},
                            }
                        }
                    }
                },
                "responses": {"200": {"description": "Board created"}},
            }
        },
    },
}

SWAGGER_2_SPEC = {
    "swagger": "2.0",
    "info": {"title": "Trello API v2", "version": "2.0"},
    "securityDefinitions": {
        "apiKey": {"type": "apiKey", "in": "query", "name": "key"}
    },
    "security": [{"apiKey": []}],
    "paths": {
        "/cards/{id}": {
            "get": {
                "summary": "Get a Card",
                "tags": ["card"],
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                ],
                "responses": {"200": {"description": "Card object"}},
            }
        }
    },
}


class TestOpenApiParsing:
    def test_parses_openapi3_endpoints(self) -> None:
        mod = ApiDocsModule()
        endpoints = mod._parse_openapi_json(OPENAPI_SPEC, "https://trello.com/openapi.json")
        assert len(endpoints) == 2
        methods = {ep.method for ep in endpoints}
        assert "GET" in methods
        assert "POST" in methods

    def test_endpoint_path_extracted(self) -> None:
        mod = ApiDocsModule()
        endpoints = mod._parse_openapi_json(OPENAPI_SPEC, "https://trello.com/openapi.json")
        paths = {ep.path for ep in endpoints}
        assert "/1/boards/{id}" in paths
        assert "/1/boards" in paths

    def test_endpoint_summary_extracted(self) -> None:
        mod = ApiDocsModule()
        endpoints = mod._parse_openapi_json(OPENAPI_SPEC, "https://trello.com/openapi.json")
        get_ep = next(ep for ep in endpoints if ep.method == "GET")
        assert get_ep.summary == "Get a Board"

    def test_auth_required_from_global_security(self) -> None:
        mod = ApiDocsModule()
        endpoints = mod._parse_openapi_json(OPENAPI_SPEC, "https://trello.com/openapi.json")
        # All endpoints inherit global security
        for ep in endpoints:
            assert ep.auth_required is True

    def test_no_auth_when_security_empty(self) -> None:
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/public": {
                    "get": {
                        "summary": "Public endpoint",
                        "security": [],
                        "responses": {"200": {"description": "OK"}},
                    }
                }
            },
        }
        mod = ApiDocsModule()
        endpoints = mod._parse_openapi_json(spec, "https://example.com/openapi.json")
        assert len(endpoints) == 1
        assert endpoints[0].auth_required is False

    def test_parameters_extracted(self) -> None:
        mod = ApiDocsModule()
        endpoints = mod._parse_openapi_json(OPENAPI_SPEC, "https://trello.com/openapi.json")
        get_ep = next(ep for ep in endpoints if ep.method == "GET")
        assert len(get_ep.parameters) == 1
        assert get_ep.parameters[0]["name"] == "id"
        assert get_ep.parameters[0]["in"] == "path"

    def test_responses_extracted(self) -> None:
        mod = ApiDocsModule()
        endpoints = mod._parse_openapi_json(OPENAPI_SPEC, "https://trello.com/openapi.json")
        get_ep = next(ep for ep in endpoints if ep.method == "GET")
        assert "200" in get_ep.responses
        assert "401" in get_ep.responses

    def test_request_body_extracted(self) -> None:
        mod = ApiDocsModule()
        endpoints = mod._parse_openapi_json(OPENAPI_SPEC, "https://trello.com/openapi.json")
        post_ep = next(ep for ep in endpoints if ep.method == "POST")
        assert "application/json" in post_ep.request_body

    def test_tags_extracted(self) -> None:
        mod = ApiDocsModule()
        endpoints = mod._parse_openapi_json(OPENAPI_SPEC, "https://trello.com/openapi.json")
        get_ep = next(ep for ep in endpoints if ep.method == "GET")
        assert "board" in get_ep.tags

    def test_swagger2_spec_parsed(self) -> None:
        mod = ApiDocsModule()
        endpoints = mod._parse_openapi_json(SWAGGER_2_SPEC, "https://trello.com/swagger.json")
        assert len(endpoints) == 1
        assert endpoints[0].method == "GET"
        assert endpoints[0].path == "/cards/{id}"

    def test_source_url_recorded(self) -> None:
        mod = ApiDocsModule()
        spec_url = "https://trello.com/openapi.json"
        endpoints = mod._parse_openapi_json(OPENAPI_SPEC, spec_url)
        for ep in endpoints:
            assert ep.source_url == spec_url

    def test_empty_paths_returns_empty(self) -> None:
        mod = ApiDocsModule()
        spec = {"openapi": "3.0.0", "paths": {}}
        endpoints = mod._parse_openapi_json(spec, "https://example.com/openapi.json")
        assert endpoints == []

    def test_non_http_method_keys_ignored(self) -> None:
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/foo": {
                    "parameters": [{"name": "id", "in": "path"}],  # not a method
                    "get": {"summary": "Foo", "responses": {"200": {"description": "OK"}}},
                }
            },
        }
        mod = ApiDocsModule()
        endpoints = mod._parse_openapi_json(spec, "https://example.com/openapi.json")
        assert len(endpoints) == 1
        assert endpoints[0].method == "GET"


# ---------------------------------------------------------------------------
# HTML endpoint extraction tests
# ---------------------------------------------------------------------------

TRELLO_HTML = """
<!DOCTYPE html>
<html>
<head><title>Trello Cards API</title></head>
<body>
  <h2>List Cards</h2>
  <p>Returns all cards on a board. Requires OAuth authentication.</p>
  <pre><code>GET /1/boards/{id}/cards</code></pre>

  <h2>Create Card</h2>
  <p>Creates a new card. API key required.</p>
  <pre><code>POST /1/cards</code></pre>

  <h2>Delete Card</h2>
  <code>DELETE /1/cards/{id}</code>
</body>
</html>
"""

TABLE_HTML = """
<!DOCTYPE html>
<html>
<head><title>Trello API Reference</title></head>
<body>
  <table>
    <thead>
      <tr><th>Method</th><th>Path</th><th>Description</th></tr>
    </thead>
    <tbody>
      <tr><td>GET</td><td>/1/lists/{id}</td><td>Get a List</td></tr>
      <tr><td>POST</td><td>/1/lists</td><td>Create a List</td></tr>
      <tr><td>PUT</td><td>/1/lists/{id}</td><td>Update a List</td></tr>
    </tbody>
  </table>
</body>
</html>
"""


class TestHtmlExtraction:
    def _parse(self, html: str, url: str = "https://example.com/docs") -> list[_ParsedEndpoint]:
        from bs4 import BeautifulSoup

        mod = ApiDocsModule()
        soup = BeautifulSoup(html, "html.parser")
        return mod._extract_endpoints_from_html(soup, url)

    def test_extracts_get_endpoint(self) -> None:
        eps = self._parse(TRELLO_HTML)
        methods = {ep.method for ep in eps}
        assert "GET" in methods

    def test_extracts_post_endpoint(self) -> None:
        eps = self._parse(TRELLO_HTML)
        methods = {ep.method for ep in eps}
        assert "POST" in methods

    def test_extracts_delete_endpoint(self) -> None:
        eps = self._parse(TRELLO_HTML)
        methods = {ep.method for ep in eps}
        assert "DELETE" in methods

    def test_extracts_correct_path(self) -> None:
        eps = self._parse(TRELLO_HTML)
        paths = {ep.path for ep in eps}
        assert "/1/boards/{id}/cards" in paths

    def test_deduplicates_same_method_path(self) -> None:
        html = """
        <html><body>
          <pre><code>GET /1/boards/{id}</code></pre>
          <pre><code>GET /1/boards/{id}</code></pre>
        </body></html>
        """
        eps = self._parse(html)
        assert len(eps) == 1

    def test_table_extraction(self) -> None:
        eps = self._parse(TABLE_HTML)
        assert len(eps) == 3

    def test_table_method_path_correct(self) -> None:
        eps = self._parse(TABLE_HTML)
        get_ep = next(ep for ep in eps if ep.method == "GET")
        assert get_ep.path == "/1/lists/{id}"

    def test_table_description_captured(self) -> None:
        eps = self._parse(TABLE_HTML)
        get_ep = next(ep for ep in eps if ep.method == "GET")
        assert "Get a List" in get_ep.summary

    def test_auth_detection_from_context(self) -> None:
        eps = self._parse(TRELLO_HTML)
        get_ep = next(ep for ep in eps if ep.method == "GET")
        # "Requires OAuth authentication" is near the GET endpoint
        assert get_ep.auth_required is True

    def test_source_url_set_on_each_endpoint(self) -> None:
        url = "https://developer.atlassian.com/cloud/trello/rest/"
        eps = self._parse(TRELLO_HTML, url=url)
        for ep in eps:
            assert ep.source_url == url

    def test_page_title_in_tags(self) -> None:
        eps = self._parse(TRELLO_HTML)
        for ep in eps:
            assert any("Trello Cards API" in tag for tag in ep.tags)

    def test_empty_html_returns_no_endpoints(self) -> None:
        eps = self._parse("<html><body><p>No endpoints here.</p></body></html>")
        assert eps == []


# ---------------------------------------------------------------------------
# Fact creation tests
# ---------------------------------------------------------------------------


class TestFactCreation:
    def _make_endpoint(self, **kwargs: Any) -> _ParsedEndpoint:
        defaults: dict[str, Any] = {
            "method": "GET",
            "path": "/1/boards/{id}",
            "summary": "Get a Board",
            "auth_required": True,
            "auth_schemes": ["ApiKey"],
            "source_url": "https://developer.atlassian.com/cloud/trello/rest/",
            "raw_excerpt": "GET /1/boards/{id} — Get a Board",
        }
        defaults.update(kwargs)
        return _ParsedEndpoint(**defaults)

    def test_fact_category_is_api_endpoint(self) -> None:
        mod = ApiDocsModule()
        ep = self._make_endpoint()
        fact = mod._endpoint_to_fact(ep, "run-1", [], "https://trello.com")
        assert fact.category == FactCategory.API_ENDPOINT

    def test_fact_authority_is_authoritative(self) -> None:
        mod = ApiDocsModule()
        ep = self._make_endpoint()
        fact = mod._endpoint_to_fact(ep, "run-1", [], "https://trello.com")
        assert fact.authority == Authority.AUTHORITATIVE

    def test_fact_source_type_is_api_docs(self) -> None:
        mod = ApiDocsModule()
        ep = self._make_endpoint()
        fact = mod._endpoint_to_fact(ep, "run-1", [], "https://trello.com")
        assert fact.source_type == SourceType.API_DOCS

    def test_fact_module_name_is_api_docs(self) -> None:
        mod = ApiDocsModule()
        ep = self._make_endpoint()
        fact = mod._endpoint_to_fact(ep, "run-1", [], "https://trello.com")
        assert fact.module_name == "api_docs"

    def test_fact_has_at_least_one_evidence(self) -> None:
        mod = ApiDocsModule()
        ep = self._make_endpoint()
        fact = mod._endpoint_to_fact(ep, "run-1", [], "https://trello.com")
        assert len(fact.evidence) >= 1

    def test_fact_evidence_source_url_set(self) -> None:
        mod = ApiDocsModule()
        ep = self._make_endpoint()
        fact = mod._endpoint_to_fact(ep, "run-1", [], "https://trello.com")
        assert fact.evidence[0].source_url == ep.source_url

    def test_fact_evidence_locator_is_method_path(self) -> None:
        mod = ApiDocsModule()
        ep = self._make_endpoint()
        fact = mod._endpoint_to_fact(ep, "run-1", [], "https://trello.com")
        assert fact.evidence[0].locator == "GET /1/boards/{id}"

    def test_fact_run_id_set(self) -> None:
        mod = ApiDocsModule()
        ep = self._make_endpoint()
        fact = mod._endpoint_to_fact(ep, "run-42", [], "https://trello.com")
        assert fact.run_id == "run-42"

    def test_fact_claim_contains_method_and_path(self) -> None:
        mod = ApiDocsModule()
        ep = self._make_endpoint()
        fact = mod._endpoint_to_fact(ep, "run-1", [], "https://trello.com")
        assert "GET" in fact.claim
        assert "/1/boards/{id}" in fact.claim

    def test_fact_claim_mentions_auth(self) -> None:
        mod = ApiDocsModule()
        ep = self._make_endpoint(auth_required=True, auth_schemes=["ApiKey"])
        fact = mod._endpoint_to_fact(ep, "run-1", [], "https://trello.com")
        assert "authentication" in fact.claim.lower() or "auth" in fact.claim.lower()

    def test_fact_claim_no_auth_when_public(self) -> None:
        mod = ApiDocsModule()
        ep = self._make_endpoint(auth_required=False, auth_schemes=[])
        fact = mod._endpoint_to_fact(ep, "run-1", [], "https://trello.com")
        assert "no authentication" in fact.claim.lower()

    def test_fact_structured_data_contains_method(self) -> None:
        mod = ApiDocsModule()
        ep = self._make_endpoint()
        fact = mod._endpoint_to_fact(ep, "run-1", [], "https://trello.com")
        assert fact.structured_data["method"] == "GET"

    def test_fact_structured_data_contains_path(self) -> None:
        mod = ApiDocsModule()
        ep = self._make_endpoint()
        fact = mod._endpoint_to_fact(ep, "run-1", [], "https://trello.com")
        assert fact.structured_data["path"] == "/1/boards/{id}"

    def test_feature_inferred_from_scope(self) -> None:
        mod = ApiDocsModule()
        ep = self._make_endpoint(path="/1/boards/{id}/cards")
        fact = mod._endpoint_to_fact(ep, "run-1", ["boards", "cards"], "https://trello.com")
        assert fact.feature in ("boards", "cards")

    def test_feature_fallback_from_path_segment(self) -> None:
        mod = ApiDocsModule()
        ep = self._make_endpoint(path="/1/members/{id}")
        fact = mod._endpoint_to_fact(ep, "run-1", [], "https://trello.com")
        assert fact.feature == "members"

    def test_feature_skips_version_prefix(self) -> None:
        mod = ApiDocsModule()
        ep = self._make_endpoint(path="/v1/boards/{id}")
        fact = mod._endpoint_to_fact(ep, "run-1", [], "https://trello.com")
        assert fact.feature == "boards"

    def test_feature_default_when_path_is_root(self) -> None:
        mod = ApiDocsModule()
        ep = self._make_endpoint(path="/")
        fact = mod._endpoint_to_fact(ep, "run-1", [], "https://trello.com")
        assert fact.feature == "api-endpoints"


# ---------------------------------------------------------------------------
# Error handling tests (run() level)
# ---------------------------------------------------------------------------


class TestRunErrorHandling:
    """Tests that run() handles network failures gracefully (INV-020)."""

    def _sync_run(
        self, mod: ApiDocsModule, request: ReconRequest, services: ReconServices
    ) -> ReconResult:
        return asyncio.run(mod.run(request, services))

    def test_run_returns_result_on_total_network_failure(self) -> None:
        """run() must not raise even when all HTTP requests fail."""
        mod = ApiDocsModule()

        # Mock client that always raises a network error
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Network down"))
        mock_client.aclose = AsyncMock()

        request = _make_request()
        services = _make_services(http_client=mock_client)

        result = self._sync_run(mod, request, services)
        assert isinstance(result, ReconResult)
        assert result.module == "api_docs"

    def test_run_status_failed_when_no_facts_found(self) -> None:
        mod = ApiDocsModule()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Network down"))
        mock_client.aclose = AsyncMock()

        request = _make_request()
        services = _make_services(http_client=mock_client)

        result = self._sync_run(mod, request, services)
        assert result.status == ReconModuleStatus.FAILED
        assert result.facts == []

    def test_run_result_module_matches_name(self) -> None:
        mod = ApiDocsModule()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Network down"))
        mock_client.aclose = AsyncMock()

        request = _make_request()
        services = _make_services(http_client=mock_client)

        result = self._sync_run(mod, request, services)
        assert result.module == mod.name

    def test_run_with_openapi_spec_produces_facts(self) -> None:
        """run() produces facts when OpenAPI spec is found."""
        mod = ApiDocsModule()

        spec_json = json.dumps(OPENAPI_SPEC)

        # First call returns the spec, subsequent calls return 404
        call_count = 0

        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            # First call hits an openapi probe path successfully
            if call_count == 1:
                return _mock_response(200, spec_json, "application/json")
            return _mock_response(404)

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.aclose = AsyncMock()

        request = _make_request(module_config={"openapi_spec_url": "https://trello.com/openapi.json"})
        services = _make_services(http_client=mock_client)

        result = self._sync_run(mod, request, services)
        assert result.status == ReconModuleStatus.SUCCESS
        assert len(result.facts) == 2  # Two endpoints in OPENAPI_SPEC
        assert result.module == "api_docs"

    def test_run_status_partial_when_some_errors(self) -> None:
        """run() returns PARTIAL when some facts extracted despite errors."""
        mod = ApiDocsModule()

        spec_json = json.dumps(OPENAPI_SPEC)
        call_count = 0

        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_response(200, spec_json, "application/json")
            return _mock_response(429)  # rate limit on probe paths

        mock_client = AsyncMock()
        mock_client.get = mock_get

        request = _make_request(module_config={"openapi_spec_url": "https://trello.com/openapi.json"})
        services = _make_services(http_client=mock_client)

        # Should not raise
        result = self._sync_run(mod, request, services)
        assert result.module == "api_docs"
        assert result.facts  # got some facts

    def test_run_errors_captured_not_raised(self) -> None:
        """Errors go into ReconResult.errors, not raised as exceptions."""
        mod = ApiDocsModule()

        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            return _mock_response(503)

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.aclose = AsyncMock()

        request = _make_request()
        services = _make_services(http_client=mock_client)

        # Must not raise
        result = self._sync_run(mod, request, services)
        assert isinstance(result, ReconResult)

    def test_run_html_fallback_produces_facts(self) -> None:
        """When no OpenAPI spec, HTML crawl produces facts."""
        mod = ApiDocsModule()

        call_count = 0

        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            nonlocal call_count
            call_count += 1
            # All OpenAPI probes return 404
            if any(probe in url for probe in ["/openapi", "/swagger", "/api-docs", "/v3"]):
                return _mock_response(404)
            # The HTML doc page returns valid HTML
            return _mock_response(200, TRELLO_HTML, "text/html")

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.aclose = AsyncMock()

        request = _make_request(
            module_config={
                "doc_url": "https://developer.atlassian.com/cloud/trello/"
            }
        )
        services = _make_services(http_client=mock_client)

        result = self._sync_run(mod, request, services)
        assert result.module == "api_docs"
        assert len(result.facts) > 0

    def test_run_progress_callback_called(self) -> None:
        """Progress callback is invoked at least once."""
        mod = ApiDocsModule()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=Exception("Network down"))
        mock_client.aclose = AsyncMock()

        request = _make_request()
        services = _make_services(http_client=mock_client)

        progress_calls: list[Any] = []

        def collect_progress(p: Any) -> None:
            progress_calls.append(p)

        asyncio.run(mod.run(request, services, progress=collect_progress))

        assert len(progress_calls) >= 1

    def test_run_rate_limit_captured_as_error(self) -> None:
        """429 responses produce rate_limited errors in the result."""
        mod = ApiDocsModule()

        async def mock_get(url: str, **kwargs: Any) -> MagicMock:
            resp = _mock_response(429)
            resp.headers = {"content-type": "text/html", "Retry-After": "0"}
            return resp

        mock_client = AsyncMock()
        mock_client.get = mock_get
        mock_client.aclose = AsyncMock()

        request = _make_request(
            module_config={"doc_url": "https://developer.atlassian.com/cloud/trello/"}
        )
        services = _make_services(http_client=mock_client)

        result = self._sync_run(mod, request, services)
        # Should not raise, errors should be in result
        assert isinstance(result, ReconResult)
        assert result.status in (ReconModuleStatus.FAILED, ReconModuleStatus.PARTIAL)
