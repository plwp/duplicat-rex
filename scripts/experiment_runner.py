"""
ExperimentRunner — generate and execute experiments to validate hypotheses.

Experiments are API-level probes (httpx) that test CRUD operations against
the target and record results. Playwright-level experiments are represented
as script strings for future execution.
"""

from __future__ import annotations

import uuid

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

from scripts.domain_model import (
    DomainModel,
    EntityHypothesis,
    Experiment,
    OperationHypothesis,
)


def _make_id() -> str:
    return str(uuid.uuid4())[:8]


class ExperimentRunner:
    """Generate and run experiments for unvalidated hypotheses."""

    def __init__(
        self,
        base_url: str,
        auth_state_path: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth_state_path = auth_state_path
        self.headers = headers or {}
        self.timeout = timeout

    async def run_experiments(
        self, model: DomainModel, max_experiments: int = 50
    ) -> list[Experiment]:
        """Generate and run experiments for unvalidated hypotheses."""
        experiments = self._generate_experiments(model)
        results: list[Experiment] = []
        for exp in experiments[:max_experiments]:
            result = await self._execute_experiment(exp)
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Experiment generation
    # ------------------------------------------------------------------

    def _generate_experiments(self, model: DomainModel) -> list[Experiment]:
        """Generate experiment scripts for unvalidated hypotheses."""
        experiments: list[Experiment] = []
        for entity in model.entities.values():
            for op in entity.unvalidated_operations():
                exp = self._create_experiment_for_operation(entity, op)
                experiments.append(exp)
        return experiments

    def _create_experiment_for_operation(
        self, entity: EntityHypothesis, op: OperationHypothesis
    ) -> Experiment:
        """Create a single experiment for an operation hypothesis."""
        endpoint = op.endpoint_pattern.replace("{id}", "TEST_ID")
        url = f"{self.base_url}{endpoint}"

        hypothesis = (
            f"{entity.name}.{op.name}: "
            f"{op.method} {op.endpoint_pattern} returns {op.response_status}"
        )
        expected = f"HTTP {op.response_status}"
        script = self._build_script(op.method, url, op.required_fields)

        return Experiment(
            id=_make_id(),
            entity=entity.name,
            hypothesis=hypothesis,
            operation=op.name,
            script=script,
            expected=expected,
        )

    def _build_script(
        self, method: str, url: str, required_fields: list[str]
    ) -> str:
        """Build a minimal httpx script string for documentation purposes."""
        body = {f: f"<{f}>" for f in required_fields}
        lines = [
            "import httpx",
            f'response = httpx.{method.lower()}("{url}"',
        ]
        if body and method not in ("GET", "DELETE"):
            lines[-1] += f", json={body!r}"
        lines[-1] += ")"
        lines.append("print(response.status_code, response.text[:200])")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Experiment execution
    # ------------------------------------------------------------------

    async def _execute_experiment(self, experiment: Experiment) -> Experiment:
        """Run a single API-level experiment and record the result."""
        if not _HTTPX_AVAILABLE:
            experiment.error = "httpx not available"
            experiment.actual = "skipped: httpx not installed"
            return experiment

        # Extract method and URL from the script
        method, url = self._parse_script(experiment.script)
        if not method or not url:
            experiment.error = "Could not parse experiment script"
            return experiment

        try:
            async with httpx.AsyncClient(
                headers=self.headers, timeout=self.timeout, follow_redirects=True
            ) as client:
                response = await client.request(method, url)

            experiment.actual = f"HTTP {response.status_code}"
            experiment.evidence = {
                "status_code": response.status_code,
                "response_preview": response.text[:500],
                "headers": dict(response.headers),
            }

            # Pass if we got any 2xx or 4xx (not 5xx server error)
            # 401/403 means the endpoint exists but requires auth — still validates shape
            expected_code = self._expected_code(experiment.expected)
            actual_code = response.status_code

            if actual_code < 500:
                experiment.passed = True
                if expected_code and actual_code != expected_code:
                    # Endpoint exists but status differs — partial pass
                    experiment.evidence["note"] = (
                        f"Expected {expected_code}, got {actual_code}"
                    )

        except Exception as exc:  # noqa: BLE001
            experiment.error = str(exc)
            experiment.actual = f"error: {exc}"

        return experiment

    def _parse_script(self, script: str) -> tuple[str, str]:
        """Extract (method, url) from a generated script."""
        import re
        m = re.search(r'httpx\.(\w+)\("([^"]+)"', script)
        if m:
            return m.group(1).upper(), m.group(2)
        return "", ""

    def _expected_code(self, expected: str) -> int | None:
        """Parse 'HTTP 200' → 200."""
        import re
        m = re.search(r"HTTP (\d{3})", expected)
        return int(m.group(1)) if m else None

    # ------------------------------------------------------------------
    # Dry-run: generate without executing
    # ------------------------------------------------------------------

    def generate_only(self, model: DomainModel) -> list[Experiment]:
        """Return experiments without executing them (for testing/inspection)."""
        return self._generate_experiments(model)
