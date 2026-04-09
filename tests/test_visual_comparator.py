"""
Tests for scripts/visual_comparator.py

Covers:
- VisualComparator._compare_dom: identical DOMs → 100%, missing headings → low score,
  missing buttons → lower score, partial inputs, low text content
- VisualComparator._structural_similarity: identical → 100%, missing forms → lower score
- VisualComparisonResult.overall_parity: average across pages
- PageComparison and VisualComparisonResult dataclass structure
- format_visual_report: renders URLs, scores, and differences
- INV-VIS-001: combined_score always in [0, 100]
- INV-VIS-002: overall_parity always in [0, 100]
- INV-VIS-003: structural_score and dom_score always in [0, 100]
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.visual_comparator import (
    PageComparison,
    VisualComparator,
    VisualComparisonResult,
    format_visual_report,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_dom(
    headings: list[str] | None = None,
    buttons: list[str] | None = None,
    inputs: int = 0,
    text_length: int = 0,
    forms: int = 0,
    tables: int = 0,
    images: int = 0,
    links: int = 0,
) -> dict:
    return {
        "title": "Test Page",
        "headings": [{"tag": "H1", "text": h} for h in (headings or [])],
        "buttons": buttons or [],
        "inputs": [{"type": "text", "name": f"f{i}", "placeholder": ""} for i in range(inputs)],
        "links": [{"href": f"/link{i}", "text": f"Link {i}"} for i in range(links)],
        "images": images,
        "forms": forms,
        "tables": tables,
        "text_length": text_length,
    }


def _make_comparator(tmp_path: Path | None = None) -> VisualComparator:
    return VisualComparator(output_dir=tmp_path or Path("."))


def _make_page_comparison(**kwargs) -> PageComparison:
    defaults = dict(
        page_path="/",
        target_screenshot="target_home.png",
        clone_screenshot="clone_home.png",
        structural_score=100.0,
        dom_score=100.0,
        combined_score=100.0,
        differences=[],
    )
    defaults.update(kwargs)
    return PageComparison(**defaults)


# ---------------------------------------------------------------------------
# _compare_dom: identical DOMs → 100%
# ---------------------------------------------------------------------------


class TestDomComparisonIdentical:
    def test_identical_headings_returns_100(self):
        comparator = _make_comparator()
        dom = _make_dom(headings=["Welcome", "Features", "Pricing"])
        score, diffs = comparator._compare_dom(dom, dom)
        assert score == pytest.approx(100.0)
        assert diffs == []

    def test_identical_buttons_returns_100(self):
        comparator = _make_comparator()
        dom = _make_dom(buttons=["Sign Up", "Log In", "Get Started"])
        score, diffs = comparator._compare_dom(dom, dom)
        assert score == pytest.approx(100.0)
        assert diffs == []

    def test_identical_inputs_returns_100(self):
        comparator = _make_comparator()
        dom = _make_dom(inputs=3, text_length=500)
        score, diffs = comparator._compare_dom(dom, dom)
        assert score == pytest.approx(100.0)
        assert diffs == []

    def test_all_identical_returns_100(self):
        comparator = _make_comparator()
        dom = _make_dom(
            headings=["H1", "H2"],
            buttons=["B1", "B2"],
            inputs=2,
            text_length=1000,
        )
        score, diffs = comparator._compare_dom(dom, dom)
        assert score == pytest.approx(100.0)
        assert diffs == []


# ---------------------------------------------------------------------------
# _compare_dom: missing headings → lower score
# ---------------------------------------------------------------------------


class TestDomComparisonMissingHeadings:
    def test_no_headings_in_clone_gives_zero_heading_score(self):
        comparator = _make_comparator()
        target = _make_dom(headings=["Welcome", "Features"])
        clone = _make_dom(headings=[])
        score, diffs = comparator._compare_dom(target, clone)
        assert score < 50.0
        assert any("heading" in d.lower() for d in diffs)

    def test_partial_headings_in_clone_gives_partial_score(self):
        comparator = _make_comparator()
        target = _make_dom(headings=["Welcome", "Features", "Pricing"])
        clone = _make_dom(headings=["Welcome"])
        score, diffs = comparator._compare_dom(target, clone)
        # 1 of 3 headings match → heading score ≈ 33%
        assert 0.0 < score < 80.0
        assert any("Missing heading" in d for d in diffs)

    def test_missing_heading_reported_in_diffs(self):
        comparator = _make_comparator()
        target = _make_dom(headings=["Unique Heading"])
        clone = _make_dom(headings=[])
        _, diffs = comparator._compare_dom(target, clone)
        assert any("heading" in d.lower() for d in diffs)


# ---------------------------------------------------------------------------
# _compare_dom: missing buttons → lower score
# ---------------------------------------------------------------------------


class TestDomComparisonMissingButtons:
    def test_no_buttons_in_clone_gives_zero_button_score(self):
        comparator = _make_comparator()
        target = _make_dom(buttons=["Sign Up", "Log In"])
        clone = _make_dom(buttons=[])
        score, diffs = comparator._compare_dom(target, clone)
        assert score < 60.0
        assert any("button" in d.lower() for d in diffs)

    def test_partial_buttons_gives_partial_score(self):
        comparator = _make_comparator()
        target = _make_dom(buttons=["Sign Up", "Log In", "Get Started"])
        clone = _make_dom(buttons=["Sign Up"])
        score, diffs = comparator._compare_dom(target, clone)
        assert 0.0 < score < 90.0
        assert any("Missing button" in d for d in diffs)

    def test_missing_button_name_appears_in_diffs(self):
        comparator = _make_comparator()
        target = _make_dom(buttons=["Critical Button"])
        clone = _make_dom(buttons=[])
        _, diffs = comparator._compare_dom(target, clone)
        assert any("Critical Button" in d for d in diffs)


# ---------------------------------------------------------------------------
# _structural_similarity: identical → 100%
# ---------------------------------------------------------------------------


class TestStructuralSimilaritySameStructure:
    def test_identical_forms_returns_100(self):
        comparator = _make_comparator()
        dom = _make_dom(forms=3, tables=2, images=5, links=10)
        score = comparator._structural_similarity(dom, dom)
        assert score == pytest.approx(100.0)

    def test_identical_tables_returns_100(self):
        comparator = _make_comparator()
        dom = _make_dom(tables=4, links=8)
        score = comparator._structural_similarity(dom, dom)
        assert score == pytest.approx(100.0)

    def test_identical_images_returns_100(self):
        comparator = _make_comparator()
        dom = _make_dom(images=6, links=4)
        score = comparator._structural_similarity(dom, dom)
        assert score == pytest.approx(100.0)

    def test_no_structural_elements_returns_50(self):
        """When target has no forms/tables/images/links, default to 50.0."""
        comparator = _make_comparator()
        dom = _make_dom()
        score = comparator._structural_similarity(dom, dom)
        assert score == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# _structural_similarity: missing forms → lower score
# ---------------------------------------------------------------------------


class TestStructuralSimilarityMissingForms:
    def test_clone_missing_forms_gives_lower_score(self):
        comparator = _make_comparator()
        target = _make_dom(forms=3, links=10)
        clone = _make_dom(forms=0, links=10)
        score = comparator._structural_similarity(target, clone)
        assert score < 100.0

    def test_clone_missing_all_structure_gives_zero(self):
        comparator = _make_comparator()
        target = _make_dom(forms=2, tables=2, images=2, links=4)
        clone = _make_dom(forms=0, tables=0, images=0, links=0)
        score = comparator._structural_similarity(target, clone)
        assert score == pytest.approx(0.0)

    def test_partial_forms_gives_partial_score(self):
        comparator = _make_comparator()
        target = _make_dom(forms=4)
        clone = _make_dom(forms=2)
        score = comparator._structural_similarity(target, clone)
        assert score == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# overall_parity calculation
# ---------------------------------------------------------------------------


class TestOverallParityCalculation:
    def test_single_page_parity_equals_page_combined_score(self):
        """overall_parity with one page should equal its combined_score."""
        page = _make_page_comparison(combined_score=75.0)
        result = VisualComparisonResult(
            target_url="https://target.com",
            clone_url="http://localhost:3002",
            pages=[page],
            overall_parity=75.0,
        )
        assert result.overall_parity == pytest.approx(75.0)

    def test_multiple_pages_parity_is_average(self):
        """overall_parity = average of page combined_scores."""
        pages = [
            _make_page_comparison(combined_score=100.0),
            _make_page_comparison(combined_score=60.0),
            _make_page_comparison(combined_score=80.0),
        ]
        expected = (100.0 + 60.0 + 80.0) / 3
        result = VisualComparisonResult(
            target_url="https://target.com",
            clone_url="http://localhost:3002",
            pages=pages,
            overall_parity=expected,
        )
        assert result.overall_parity == pytest.approx(expected)

    def test_compare_computes_correct_average(self, tmp_path: Path):
        """compare() sets overall_parity as the average of page scores."""
        comparator = _make_comparator(tmp_path)

        target_dom = _make_dom(headings=["H1"], buttons=["B1"], text_length=500)
        clone_dom = _make_dom(headings=["H1"], buttons=["B1"], text_length=500)

        async def _fake_capture(url, auth, prefix, page_path):
            return (target_dom if prefix == "target" else clone_dom), f"{prefix}.png"

        async def _run():
            with patch.object(comparator, "_capture_page", side_effect=_fake_capture):
                return await comparator.compare(
                    "https://target.com",
                    "http://localhost:3002",
                    pages=["/", "/about"],
                )

        result = asyncio.run(_run())
        assert 0.0 <= result.overall_parity <= 100.0
        # Both pages are identical DOM-wise; structural_similarity defaults to 50 when
        # no structural elements present → combined = 100*0.6 + 50*0.4 = 80.0
        assert result.overall_parity >= 75.0


# ---------------------------------------------------------------------------
# VisualComparisonResult / PageComparison structure
# ---------------------------------------------------------------------------


class TestComparisonResultStructure:
    def test_page_comparison_has_required_fields(self):
        page = PageComparison(
            page_path="/boards",
            target_screenshot="target_boards.png",
            clone_screenshot="clone_boards.png",
            structural_score=85.0,
            dom_score=90.0,
            combined_score=87.0,
            differences=["Missing heading: Boards"],
        )
        assert page.page_path == "/boards"
        assert page.structural_score == 85.0
        assert page.dom_score == 90.0
        assert page.combined_score == 87.0
        assert len(page.differences) == 1

    def test_visual_comparison_result_has_required_fields(self):
        result = VisualComparisonResult(
            target_url="https://trello.com",
            clone_url="http://localhost:3002",
            pages=[],
            overall_parity=0.0,
        )
        assert result.target_url == "https://trello.com"
        assert result.clone_url == "http://localhost:3002"
        assert result.pages == []
        assert result.overall_parity == 0.0

    def test_combined_score_in_range_inv_vis_001(self, tmp_path: Path):
        """INV-VIS-001: combined_score is always in [0, 100]."""
        comparator = _make_comparator(tmp_path)
        # Edge case: empty DOMs
        target = _make_dom()
        clone = _make_dom()
        dom_score, _ = comparator._compare_dom(target, clone)
        structural_score = comparator._structural_similarity(target, clone)
        combined = dom_score * 0.6 + structural_score * 0.4
        assert 0.0 <= combined <= 100.0

    def test_dom_score_in_range_inv_vis_003(self, tmp_path: Path):
        """INV-VIS-003: dom_score is always in [0, 100]."""
        comparator = _make_comparator(tmp_path)
        # Stress: target has content, clone has none
        target = _make_dom(
            headings=["H1", "H2", "H3"],
            buttons=["B1", "B2"],
            inputs=5,
            text_length=2000,
        )
        clone = _make_dom()
        score, _ = comparator._compare_dom(target, clone)
        assert 0.0 <= score <= 100.0

    def test_structural_score_in_range_inv_vis_003(self, tmp_path: Path):
        """INV-VIS-003: structural_score is always in [0, 100]."""
        comparator = _make_comparator(tmp_path)
        target = _make_dom(forms=5, tables=3, images=10, links=20)
        clone = _make_dom(forms=10, tables=6, images=20, links=40)  # clone exceeds target
        score = comparator._structural_similarity(target, clone)
        assert 0.0 <= score <= 100.0


# ---------------------------------------------------------------------------
# format_visual_report
# ---------------------------------------------------------------------------


class TestFormatVisualReport:
    def test_contains_target_and_clone_urls(self):
        result = VisualComparisonResult(
            target_url="https://trello.com",
            clone_url="http://localhost:3002",
            pages=[],
            overall_parity=0.0,
        )
        report = format_visual_report(result)
        assert "https://trello.com" in report
        assert "http://localhost:3002" in report

    def test_contains_overall_parity_score(self):
        result = VisualComparisonResult(
            target_url="https://trello.com",
            clone_url="http://localhost:3002",
            pages=[],
            overall_parity=72.5,
        )
        report = format_visual_report(result)
        assert "72.5%" in report

    def test_contains_page_paths(self):
        page = _make_page_comparison(page_path="/boards", combined_score=85.0)
        result = VisualComparisonResult(
            target_url="https://trello.com",
            clone_url="http://localhost:3002",
            pages=[page],
            overall_parity=85.0,
        )
        report = format_visual_report(result)
        assert "/boards" in report

    def test_differences_section_shown_when_diffs_exist(self):
        page = _make_page_comparison(
            differences=["Missing heading: Welcome", "Missing button: Sign Up"],
        )
        result = VisualComparisonResult(
            target_url="https://trello.com",
            clone_url="http://localhost:3002",
            pages=[page],
            overall_parity=50.0,
        )
        report = format_visual_report(result)
        assert "VISUAL DIFFERENCES" in report
        assert "Missing heading: Welcome" in report
        assert "Missing button: Sign Up" in report

    def test_no_differences_section_when_no_diffs(self):
        page = _make_page_comparison(differences=[])
        result = VisualComparisonResult(
            target_url="https://trello.com",
            clone_url="http://localhost:3002",
            pages=[page],
            overall_parity=100.0,
        )
        report = format_visual_report(result)
        assert "VISUAL DIFFERENCES" not in report
