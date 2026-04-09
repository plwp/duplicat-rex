"""
VisualComparator — screenshot-based parity measurement.

For each page/component, takes screenshots of the target and clone,
then compares them structurally (not pixel-perfect) to score parity.

Invariants:
    INV-VIS-001: combined_score is always in [0, 100].
    INV-VIS-002: overall_parity is always in [0, 100].
    INV-VIS-003: structural_score and dom_score are always in [0, 100].
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PageComparison:
    page_path: str  # e.g. "/boards"
    target_screenshot: str
    clone_screenshot: str
    structural_score: float  # 0-100
    dom_score: float  # 0-100 based on DOM similarity
    combined_score: float  # weighted average
    differences: list[str]  # what's different


@dataclass
class VisualComparisonResult:
    target_url: str
    clone_url: str
    pages: list[PageComparison] = field(default_factory=list)
    overall_parity: float = 0.0  # 0-100


class VisualComparator:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir

    async def compare(
        self,
        target_url: str,
        clone_url: str,
        pages: list[str],  # paths to compare, e.g. ["/", "/boards", "/pricing"]
        target_auth_state: str | None = None,
        clone_auth_state: str | None = None,
    ) -> VisualComparisonResult:
        """Compare target and clone by navigating pages and comparing screenshots + DOM."""
        comparisons = []

        for page_path in pages:
            comparison = await self._compare_page(
                target_url, clone_url, page_path,
                target_auth_state, clone_auth_state,
            )
            comparisons.append(comparison)

        overall = (
            sum(c.combined_score for c in comparisons) / len(comparisons) if comparisons else 0
        )
        # Clamp to [0, 100] (INV-VIS-002)
        overall = max(0.0, min(100.0, overall))
        return VisualComparisonResult(
            target_url=target_url,
            clone_url=clone_url,
            pages=comparisons,
            overall_parity=overall,
        )

    async def _compare_page(
        self,
        target_url: str,
        clone_url: str,
        page_path: str,
        target_auth: str | None,
        clone_auth: str | None,
    ) -> PageComparison:
        """Compare a single page between target and clone."""
        # 1. Screenshot target
        target_dom, target_screenshot = await self._capture_page(
            f"{target_url}{page_path}", target_auth, "target", page_path,
        )

        # 2. Screenshot clone
        clone_dom, clone_screenshot = await self._capture_page(
            f"{clone_url}{page_path}", clone_auth, "clone", page_path,
        )

        # 3. Compare DOM structure
        dom_score, differences = self._compare_dom(target_dom, clone_dom)

        # 4. Structural similarity (element count, heading structure, form presence)
        structural_score = self._structural_similarity(target_dom, clone_dom)

        # Clamp scores (INV-VIS-001, INV-VIS-003)
        dom_score = max(0.0, min(100.0, dom_score))
        structural_score = max(0.0, min(100.0, structural_score))
        combined = max(0.0, min(100.0, dom_score * 0.6 + structural_score * 0.4))

        return PageComparison(
            page_path=page_path,
            target_screenshot=target_screenshot,
            clone_screenshot=clone_screenshot,
            structural_score=structural_score,
            dom_score=dom_score,
            combined_score=combined,
            differences=differences,
        )

    async def _capture_page(
        self,
        url: str,
        auth_state: str | None,
        prefix: str,
        page_path: str,
    ) -> tuple[dict, str]:
        """Navigate to page, capture DOM summary and screenshot."""
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True)

        context_kwargs: dict = {"viewport": {"width": 1440, "height": 900}}
        if auth_state and Path(auth_state).exists():
            context_kwargs["storage_state"] = auth_state

        context = await browser.new_context(**context_kwargs)
        page = await context.new_page()

        try:
            await page.goto(url, timeout=15000)
            await page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(2)

            # Capture DOM summary
            dom = await page.evaluate(
                """() => {
                const r = {
                    title: document.title,
                    headings: [],
                    buttons: [],
                    inputs: [],
                    links: [],
                    images: 0,
                    forms: 0,
                    tables: 0,
                    text_length: document.body?.innerText?.length || 0,
                };
                document.querySelectorAll('h1,h2,h3,h4').forEach(h => {
                    const t = h.innerText.trim().substring(0, 100);
                    r.headings.push({tag: h.tagName, text: t});
                });
                document.querySelectorAll('button,[role="button"]').forEach(b =>
                    r.buttons.push(b.innerText.trim().substring(0, 50))
                );
                document.querySelectorAll('input,textarea,select').forEach(i =>
                    r.inputs.push({
                        type: i.type || i.tagName,
                        name: i.name,
                        placeholder: i.placeholder,
                    })
                );
                document.querySelectorAll('a[href]').forEach(a => {
                    const t = a.innerText.trim().substring(0, 50);
                    r.links.push({href: a.getAttribute('href'), text: t});
                });
                r.images = document.querySelectorAll('img').length;
                r.forms = document.querySelectorAll('form').length;
                r.tables = document.querySelectorAll('table').length;
                return r;
            }"""
            )

            # Screenshot
            screenshot_dir = self.output_dir / "visual_comparison"
            screenshot_dir.mkdir(parents=True, exist_ok=True)
            safe_path = page_path.strip("/").replace("/", "_") or "home"
            screenshot_path = str(screenshot_dir / f"{prefix}_{safe_path}.png")
            await page.screenshot(path=screenshot_path, full_page=True)

            return dom, screenshot_path
        finally:
            await browser.close()
            await pw.stop()

    def _compare_dom(self, target_dom: dict, clone_dom: dict) -> tuple[float, list[str]]:
        """Compare DOM summaries and return (score, differences)."""
        differences: list[str] = []
        scores: list[float] = []

        # Compare headings
        t_headings = {h["text"] for h in target_dom.get("headings", [])}
        c_headings = {h["text"] for h in clone_dom.get("headings", [])}
        if t_headings and c_headings:
            overlap = len(t_headings & c_headings) / max(len(t_headings), len(c_headings))
            scores.append(overlap * 100)
            for missing in t_headings - c_headings:
                differences.append(f"Missing heading: {missing}")
        elif t_headings:
            scores.append(0.0)
            differences.append(f"Clone has no headings (target has {len(t_headings)})")

        # Compare buttons
        t_buttons = set(target_dom.get("buttons", []))
        c_buttons = set(clone_dom.get("buttons", []))
        if t_buttons:
            overlap = len(t_buttons & c_buttons) / max(len(t_buttons), 1)
            scores.append(overlap * 100)
            for missing in t_buttons - c_buttons:
                differences.append(f"Missing button: {missing}")

        # Compare inputs
        t_inputs = len(target_dom.get("inputs", []))
        c_inputs = len(clone_dom.get("inputs", []))
        if t_inputs > 0:
            ratio = min(c_inputs / t_inputs, 1.0)
            scores.append(ratio * 100)
            if c_inputs < t_inputs:
                differences.append(f"Fewer inputs: {c_inputs} vs {t_inputs}")

        # Compare content amount
        t_text = target_dom.get("text_length", 0)
        c_text = clone_dom.get("text_length", 0)
        if t_text > 100:
            ratio = min(c_text / max(t_text, 1), 1.5)  # cap at 150%
            scores.append(min(ratio * 100, 100))
            if c_text < t_text * 0.5:
                differences.append(f"Much less content: {c_text} vs {t_text} chars")

        avg_score = sum(scores) / len(scores) if scores else 0.0
        return avg_score, differences

    def _structural_similarity(self, target_dom: dict, clone_dom: dict) -> float:
        """Compare structural elements (forms, tables, images, link count)."""
        comparisons: list[float] = []
        for key in ["forms", "tables", "images"]:
            t_val = target_dom.get(key, 0)
            c_val = clone_dom.get(key, 0)
            if t_val > 0:
                comparisons.append(min(c_val / t_val, 1.0) * 100)

        t_links = len(target_dom.get("links", []))
        c_links = len(clone_dom.get("links", []))
        if t_links > 0:
            comparisons.append(min(c_links / t_links, 1.0) * 100)

        return sum(comparisons) / len(comparisons) if comparisons else 50.0


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def format_visual_report(result: VisualComparisonResult) -> str:
    """Render a human-readable visual comparison report."""
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("VISUAL COMPARISON REPORT")
    lines.append("=" * 70)
    lines.append(f"Target: {result.target_url}")
    lines.append(f"Clone:  {result.clone_url}")
    lines.append("")
    lines.append(f"Overall Visual Parity: {result.overall_parity:.1f}%")
    lines.append("")

    if result.pages:
        lines.append("Page Breakdown:")
        for page in result.pages:
            bar = _progress_bar(page.combined_score)
            lines.append(
                f"  {page.page_path:<30} {bar}  {page.combined_score:5.1f}%"
                f"  (dom={page.dom_score:.0f}%, struct={page.structural_score:.0f}%)"
            )
        lines.append("")

    # Differences
    has_diffs = any(p.differences for p in result.pages)
    if has_diffs:
        lines.append("─" * 70)
        lines.append("VISUAL DIFFERENCES — Action Required:")
        lines.append("─" * 70)
        for page in result.pages:
            if page.differences:
                lines.append(f"\n[{page.page_path}]")
                for diff in page.differences:
                    lines.append(f"  - {diff}")

    lines.append("")
    lines.append("=" * 70)
    return "\n".join(lines)


def _progress_bar(score: float, width: int = 20) -> str:
    """Render a compact ASCII progress bar for a 0-100 score."""
    filled = round(score / 100 * width)
    return "[" + "#" * filled + "." * (width - filled) + "]"
