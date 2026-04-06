"""
Tests for scripts/test_generator.py

Covers:
- API test generation from spec items with api_contracts
- E2E test generation from spec items with user_flows / ui_patterns
- Auth test generation from spec items with auth_scenarios
- Schema test generation from spec items with data_models
- Dual-execution BASE_URL fixture is present in every generated file
- Tolerated variance: volatile fields excluded from assertions
- Output file structure: tests/conformance/
- INV-TG-001: every generated file is valid Python
- INV-TG-003: spec_coverage reflects which features have tests
- Empty spec content produces no files
- by_category counts are accurate
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.models import (
    BundleStatus,
    Confidence,
    EvidenceRef,
    Fact,
    FactCategory,
    SourceType,
    SpecBundle,
    SpecItem,
)
from scripts.spec_store import SpecStore
from scripts.test_generator import (
    GeneratedTestSuite,
    TestGenerator,
    _feature_slug,
    _validate_python_syntax,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_evidence(url: str = "https://example.com") -> EvidenceRef:
    return EvidenceRef(
        source_url=url,
        source_title="Test",
        captured_at="2024-01-01T00:00:00+00:00",
    )


def make_fact(
    *,
    feature: str = "boards",
    category: FactCategory = FactCategory.API_ENDPOINT,
    claim: str = "The API returns a list of boards",
) -> Fact:
    return Fact(
        feature=feature,
        category=category,
        claim=claim,
        evidence=[make_evidence()],
        source_type=SourceType.API_DOCS,
    )


def make_store_and_generator(tmp_path: Path) -> tuple[SpecStore, TestGenerator]:
    store = SpecStore(tmp_path / "specstore_root")
    gen = TestGenerator(store)
    return store, gen


def make_bundle(
    spec_items: list[SpecItem],
    scope: list[str] | None = None,
) -> SpecBundle:
    features = scope or list({item.feature for item in spec_items})
    return SpecBundle(
        target="example.com",
        scope=features,
        status=BundleStatus.DRAFT,
        spec_items=spec_items,
        fact_ids=[],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_store(tmp_path: Path) -> tuple[SpecStore, TestGenerator]:
    return make_store_and_generator(tmp_path)


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    d = tmp_path / "output"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Test: feature_slug utility
# ---------------------------------------------------------------------------


def test_feature_slug_basic() -> None:
    assert _feature_slug("boards") == "boards"


def test_feature_slug_hyphen() -> None:
    assert _feature_slug("drag-drop") == "drag_drop"


def test_feature_slug_space() -> None:
    assert _feature_slug("user flow") == "user_flow"


def test_feature_slug_dot() -> None:
    assert _feature_slug("auth.sso") == "auth_sso"


# ---------------------------------------------------------------------------
# Test: validate_python_syntax
# ---------------------------------------------------------------------------


def test_validate_python_syntax_valid(tmp_path: Path) -> None:
    p = tmp_path / "test_valid.py"
    p.write_text("def foo():\n    pass\n")
    _validate_python_syntax(p)  # should not raise


def test_validate_python_syntax_invalid(tmp_path: Path) -> None:
    p = tmp_path / "test_bad.py"
    p.write_text("def foo(\n    pass\n")  # broken syntax
    with pytest.raises(SyntaxError):
        _validate_python_syntax(p)


# ---------------------------------------------------------------------------
# Test: API test generation
# ---------------------------------------------------------------------------


def test_generate_api_tests_creates_file(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    item = SpecItem(
        feature="boards",
        spec_type="api_contract",
        content={
            "api_contracts": [
                {
                    "endpoint": "GET /api/boards",
                    "requires": ["user is authenticated"],
                    "ensures": ["returns list of boards"],
                }
            ]
        },
        supporting_fact_ids=[],
        confidence=Confidence.HIGH,
    )
    bundle = make_bundle([item])
    suite = gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")

    assert suite.by_category["api"] == 1
    assert suite.total_tests >= 1
    api_file = output_dir / "tests" / "conformance" / "test_api_boards.py"
    assert api_file.exists()
    assert api_file in suite.test_files


def test_generate_api_tests_content(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    item = SpecItem(
        feature="cards",
        spec_type="api_contract",
        content={
            "api_contracts": [
                {"endpoint": "POST /api/cards", "requires": [], "ensures": ["card created"]}
            ]
        },
        supporting_fact_ids=[],
    )
    bundle = make_bundle([item])
    gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")

    code = (output_dir / "tests" / "conformance" / "test_api_cards.py").read_text()
    assert "def test_api_cards_0" in code
    assert "httpx.post" in code
    assert "base_url" in code


def test_generate_api_tests_multiple_contracts(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    item = SpecItem(
        feature="boards",
        spec_type="api_contract",
        content={
            "api_contracts": [
                {"endpoint": "GET /api/boards"},
                {"endpoint": "POST /api/boards"},
                {"endpoint": "DELETE /api/boards/{id}"},
            ]
        },
        supporting_fact_ids=[],
    )
    bundle = make_bundle([item])
    suite = gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")
    assert suite.by_category["api"] == 3


def test_generate_no_api_contracts_produces_no_api_file(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    item = SpecItem(
        feature="boards",
        spec_type="synthesised_spec",
        content={"summary": "just a summary"},
        supporting_fact_ids=[],
    )
    bundle = make_bundle([item])
    suite = gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")
    assert suite.by_category["api"] == 0
    api_file = output_dir / "tests" / "conformance" / "test_api_boards.py"
    assert not api_file.exists()


# ---------------------------------------------------------------------------
# Test: E2E test generation
# ---------------------------------------------------------------------------


def test_generate_e2e_tests_from_user_flows(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    item = SpecItem(
        feature="boards",
        spec_type="user_flow",
        content={
            "user_flows": [
                {
                    "name": "Create board",
                    "description": "User creates a new board",
                    "steps": ["click +", "enter name", "submit"],
                }
            ]
        },
        supporting_fact_ids=[],
    )
    bundle = make_bundle([item])
    suite = gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")

    assert suite.by_category["e2e"] == 1
    e2e_file = output_dir / "tests" / "conformance" / "test_e2e_boards.py"
    assert e2e_file.exists()
    code = e2e_file.read_text()
    assert "playwright" in code.lower() or "Page" in code
    assert "base_url" in code


def test_generate_e2e_tests_from_ui_patterns_fallback(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    item = SpecItem(
        feature="sidebar",
        spec_type="synthesised_spec",
        content={
            "ui_patterns": [
                {
                    "component": "SidebarNav",
                    "behavior": "Collapses on mobile",
                    "states": ["open", "closed"],
                }
            ]
        },
        supporting_fact_ids=[],
    )
    bundle = make_bundle([item])
    suite = gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")

    assert suite.by_category["e2e"] == 1
    e2e_file = output_dir / "tests" / "conformance" / "test_e2e_sidebar.py"
    assert e2e_file.exists()


def test_generate_e2e_test_content(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    item = SpecItem(
        feature="login",
        spec_type="user_flow",
        content={
            "user_flows": [
                {"name": "Login flow", "description": "User logs in with credentials"}
            ]
        },
        supporting_fact_ids=[],
    )
    bundle = make_bundle([item])
    gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")

    code = (output_dir / "tests" / "conformance" / "test_e2e_login.py").read_text()
    assert "def test_e2e_login_0" in code
    assert "page.goto" in code
    assert "expect" in code


# ---------------------------------------------------------------------------
# Test: Auth test generation
# ---------------------------------------------------------------------------


def test_generate_auth_tests(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    item = SpecItem(
        feature="auth",
        spec_type="auth_spec",
        content={
            "auth_scenarios": [
                {
                    "scenario": "Unauthenticated access to protected route",
                    "method": "GET",
                    "path": "/api/me",
                    "expected_status": 401,
                },
                {
                    "scenario": "Access without required role",
                    "method": "DELETE",
                    "path": "/api/admin/users",
                    "expected_status": 403,
                },
            ]
        },
        supporting_fact_ids=[],
    )
    bundle = make_bundle([item])
    suite = gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")

    assert suite.by_category["auth"] == 2
    auth_file = output_dir / "tests" / "conformance" / "test_auth_auth.py"
    assert auth_file.exists()
    code = auth_file.read_text()
    assert "def test_auth_auth_0" in code
    assert "401" in code or "403" in code
    assert "httpx" in code


def test_generate_auth_tests_content_checks_status(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    item = SpecItem(
        feature="api-keys",
        spec_type="auth_spec",
        content={
            "auth_scenarios": [
                {
                    "scenario": "Missing API key",
                    "method": "GET",
                    "path": "/api/data",
                    "expected_status": 401,
                }
            ]
        },
        supporting_fact_ids=[],
    )
    bundle = make_bundle([item])
    gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")

    code = (output_dir / "tests" / "conformance" / "test_auth_api_keys.py").read_text()
    assert "assert response.status_code in" in code
    assert "401" in code


def test_generate_no_auth_scenarios_produces_no_auth_file(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    item = SpecItem(
        feature="boards",
        spec_type="synthesised_spec",
        content={"api_contracts": [{"endpoint": "GET /api/boards"}]},
        supporting_fact_ids=[],
    )
    bundle = make_bundle([item])
    suite = gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")
    assert suite.by_category["auth"] == 0


# ---------------------------------------------------------------------------
# Test: Schema/data-model test generation
# ---------------------------------------------------------------------------


def test_generate_schema_tests(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    item = SpecItem(
        feature="boards",
        spec_type="data_model",
        content={
            "data_models": [
                {
                    "entity": "Board",
                    "fields": {"id": "string", "title": "string", "created_at": "datetime"},
                    "constraints": ["title is required", "title max 255 chars"],
                }
            ]
        },
        supporting_fact_ids=[],
    )
    bundle = make_bundle([item])
    suite = gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")

    assert suite.by_category["schema"] == 1
    schema_file = output_dir / "tests" / "conformance" / "test_schema_boards.py"
    assert schema_file.exists()
    code = schema_file.read_text()
    assert "def test_schema_boards_0" in code
    assert "httpx" in code
    assert "base_url" in code


def test_generate_schema_tests_field_assertions(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    item = SpecItem(
        feature="cards",
        spec_type="data_model",
        content={
            "data_models": [
                {
                    "entity": "Card",
                    "fields": {"title": "string", "description": "string", "position": "int"},
                    "constraints": [],
                }
            ]
        },
        supporting_fact_ids=[],
    )
    bundle = make_bundle([item])
    gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")

    code = (output_dir / "tests" / "conformance" / "test_schema_cards.py").read_text()
    # At least some field names from the data model should appear
    assert "title" in code or "description" in code or "position" in code


# ---------------------------------------------------------------------------
# Test: Dual-execution fixture
# ---------------------------------------------------------------------------


def test_dual_execution_fixture_in_api_file(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    item = SpecItem(
        feature="boards",
        spec_type="api_contract",
        content={"api_contracts": [{"endpoint": "GET /api/boards"}]},
        supporting_fact_ids=[],
    )
    bundle = make_bundle([item])
    gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")

    code = (output_dir / "tests" / "conformance" / "test_api_boards.py").read_text()
    # BASE_URL env var must be referenced in the fixture
    assert "BASE_URL" in code
    assert "os.environ" in code
    assert "@pytest.fixture" in code


def test_dual_execution_fixture_in_e2e_file(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    item = SpecItem(
        feature="boards",
        spec_type="user_flow",
        content={"user_flows": [{"name": "view board", "description": "user sees board"}]},
        supporting_fact_ids=[],
    )
    bundle = make_bundle([item])
    gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")

    code = (output_dir / "tests" / "conformance" / "test_e2e_boards.py").read_text()
    assert "BASE_URL" in code
    assert "@pytest.fixture" in code


def test_dual_execution_fixture_in_auth_file(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    item = SpecItem(
        feature="auth",
        spec_type="auth_spec",
        content={"auth_scenarios": [{"scenario": "No token", "path": "/api/me"}]},
        supporting_fact_ids=[],
    )
    bundle = make_bundle([item])
    gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")

    code = (output_dir / "tests" / "conformance" / "test_auth_auth.py").read_text()
    assert "BASE_URL" in code
    assert "@pytest.fixture" in code


# ---------------------------------------------------------------------------
# Test: Tolerated variance
# ---------------------------------------------------------------------------


def test_tolerated_variance_volatile_fields_in_api_file(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    item = SpecItem(
        feature="boards",
        spec_type="api_contract",
        content={"api_contracts": [{"endpoint": "GET /api/boards"}]},
        supporting_fact_ids=[],
    )
    bundle = make_bundle([item])
    gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")

    code = (output_dir / "tests" / "conformance" / "test_api_boards.py").read_text()
    # The assert_equivalent helper and _VOLATILE_FIELDS must be present
    assert "assert_equivalent" in code
    assert "_VOLATILE_FIELDS" in code
    assert "created_at" in code
    assert '"id"' in code or "'id'" in code


def test_assert_equivalent_strips_volatile_fields() -> None:
    """Verify the assert_equivalent logic would strip volatile fields."""
    # Simulate what the generated code does
    volatile = frozenset(["id", "uuid", "created_at", "updated_at", "modified_at",
                          "timestamp", "token", "session_id", "request_id", "trace_id",
                          "etag", "last_modified"])

    def strip(obj: object, _ignore: frozenset) -> object:
        if isinstance(obj, dict):
            return {k: strip(v, _ignore) for k, v in obj.items() if k not in _ignore}
        if isinstance(obj, list):
            return [strip(i, _ignore) for i in obj]
        return obj

    target_resp = {"id": "abc-123", "title": "My Board", "created_at": "2024-01-01T00:00:00Z"}
    clone_resp = {"id": "xyz-789", "title": "My Board", "created_at": "2024-06-15T12:00:00Z"}

    stripped_t = strip(target_resp, volatile)
    stripped_c = strip(clone_resp, volatile)

    # IDs and timestamps stripped — only stable fields remain
    assert stripped_t == stripped_c == {"title": "My Board"}


def test_assert_equivalent_detects_real_differences() -> None:
    """Verify assert_equivalent catches real content differences."""
    volatile = frozenset(["id", "created_at"])

    def strip(obj: object, _ignore: frozenset) -> object:
        if isinstance(obj, dict):
            return {k: strip(v, _ignore) for k, v in obj.items() if k not in _ignore}
        if isinstance(obj, list):
            return [strip(i, _ignore) for i in obj]
        return obj

    target_resp = {"id": "1", "title": "Board A", "created_at": "2024-01-01"}
    clone_resp = {"id": "2", "title": "Board B", "created_at": "2024-06-01"}

    stripped_t = strip(target_resp, volatile)
    stripped_c = strip(clone_resp, volatile)

    assert stripped_t != stripped_c  # "Board A" != "Board B"


# ---------------------------------------------------------------------------
# Test: Output file structure
# ---------------------------------------------------------------------------


def test_output_directory_structure(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    item = SpecItem(
        feature="boards",
        spec_type="api_contract",
        content={"api_contracts": [{"endpoint": "GET /api/boards"}]},
        supporting_fact_ids=[],
    )
    bundle = make_bundle([item])
    suite = gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")

    assert (output_dir / "tests" / "conformance").is_dir()
    for f in suite.test_files:
        assert f.parent == output_dir / "tests" / "conformance"
        assert f.name.startswith("test_")
        assert f.suffix == ".py"


def test_spec_coverage_true_when_tests_generated(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    item = SpecItem(
        feature="boards",
        spec_type="api_contract",
        content={"api_contracts": [{"endpoint": "GET /api/boards"}]},
        supporting_fact_ids=[],
    )
    bundle = make_bundle([item], scope=["boards"])
    suite = gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")

    assert suite.spec_coverage["boards"] is True


def test_spec_coverage_false_when_no_tests(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    item = SpecItem(
        feature="boards",
        spec_type="synthesised_spec",
        content={"summary": "Just a summary, no contracts"},
        supporting_fact_ids=[],
    )
    bundle = make_bundle([item], scope=["boards"])
    suite = gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")

    assert suite.spec_coverage["boards"] is False


def test_generated_files_are_valid_python(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    """INV-TG-001: every generated file must parse cleanly."""
    _, gen = tmp_store
    items = [
        SpecItem(
            feature="boards",
            spec_type="api_contract",
            content={
                "api_contracts": [{"endpoint": "GET /api/boards"}],
                "user_flows": [{"name": "View boards", "description": "User sees board list"}],
                "auth_scenarios": [{"scenario": "No auth", "path": "/api/boards"}],
                "data_models": [
                    {"entity": "Board", "fields": {"title": "string"}, "constraints": []},
                ],
            },
            supporting_fact_ids=[],
        )
    ]
    bundle = make_bundle(items)
    suite = gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")

    assert len(suite.test_files) >= 1
    for path in suite.test_files:
        src = path.read_text(encoding="utf-8")
        # This must not raise
        ast.parse(src)


def test_empty_bundle_produces_empty_suite(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    bundle = make_bundle([], scope=["boards"])
    suite = gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")

    assert suite.total_tests == 0
    assert suite.test_files == []
    assert suite.spec_coverage == {"boards": False}


def test_multiple_features_separate_files(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    items = [
        SpecItem(
            feature="boards",
            spec_type="api_contract",
            content={"api_contracts": [{"endpoint": "GET /api/boards"}]},
            supporting_fact_ids=[],
        ),
        SpecItem(
            feature="cards",
            spec_type="api_contract",
            content={"api_contracts": [{"endpoint": "GET /api/cards"}]},
            supporting_fact_ids=[],
        ),
    ]
    bundle = make_bundle(items, scope=["boards", "cards"])
    suite = gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")

    conformance_dir = output_dir / "tests" / "conformance"
    assert (conformance_dir / "test_api_boards.py").exists()
    assert (conformance_dir / "test_api_cards.py").exists()
    assert suite.spec_coverage["boards"] is True
    assert suite.spec_coverage["cards"] is True
    assert suite.by_category["api"] == 2


def test_by_category_totals_match_total_tests(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    item = SpecItem(
        feature="boards",
        spec_type="full_spec",
        content={
            "api_contracts": [{"endpoint": "GET /api/boards"}, {"endpoint": "POST /api/boards"}],
            "user_flows": [{"name": "view", "description": "user views boards"}],
            "auth_scenarios": [{"scenario": "no auth", "path": "/api/boards"}],
            "data_models": [{"entity": "Board", "fields": {}, "constraints": []}],
        },
        supporting_fact_ids=[],
    )
    bundle = make_bundle([item])
    suite = gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")

    assert suite.total_tests == sum(suite.by_category.values())
    assert suite.by_category["api"] == 2
    assert suite.by_category["e2e"] == 1
    assert suite.by_category["auth"] == 1
    assert suite.by_category["schema"] == 1


def test_generated_suite_dataclass_fields(
    tmp_store: tuple[SpecStore, TestGenerator],
    output_dir: Path,
) -> None:
    _, gen = tmp_store
    item = SpecItem(
        feature="boards",
        spec_type="api_contract",
        content={"api_contracts": [{"endpoint": "GET /api/boards"}]},
        supporting_fact_ids=[],
    )
    bundle = make_bundle([item])
    suite = gen.generate(bundle, output_dir=output_dir, target_url="http://t", clone_url="http://c")

    assert isinstance(suite, GeneratedTestSuite)
    assert isinstance(suite.output_dir, Path)
    assert isinstance(suite.test_files, list)
    assert isinstance(suite.total_tests, int)
    assert isinstance(suite.by_category, dict)
    assert isinstance(suite.spec_coverage, dict)
    assert set(suite.by_category.keys()) == {"api", "e2e", "auth", "schema"}
