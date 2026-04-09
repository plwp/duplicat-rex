"""Duplicat-Rex CLI — entry point."""

import asyncio
import getpass
from pathlib import Path

import typer

import scripts.keychain as keychain_module
from scripts.compare import BehavioralComparator, format_report
from scripts.converge import ConvergenceConfig, ConvergenceOrchestrator
from scripts.duplicate import DuplicateConfig, DuplicatePipeline
from scripts.gap_analyzer import GapAnalyzer
from scripts.keychain import (
    DEFAULT_SERVICE,
    KNOWN_KEYS,
    delete_secret,
    has_secret,
    list_secrets,
    set_secret,
)
from scripts.models import DependencyEdge, ScopeNode
from scripts.models import Scope as ModelsScope
from scripts.recon.base import ReconProgress
from scripts.recon.orchestrator import ReconOrchestrator, ReconReport
from scripts.scope import Scope as ParsedScope
from scripts.scope import freeze_scope, parse_scope
from scripts.spec_store import SpecStore

app = typer.Typer(
    name="duplicat-rex",
    help="Agentic SaaS reverse-engineering engine.",
    no_args_is_help=True,
)

secrets_app = typer.Typer(help="Manage credentials stored in the system keyring.")
app.add_typer(secrets_app, name="secrets")


def _bridge_scope(
    parsed: ParsedScope | None,
    target: str,
) -> ModelsScope:
    """Convert scripts.scope.Scope to scripts.models.Scope."""
    if parsed is None:
        return ModelsScope(target=target, raw_input="all")

    requested = []
    resolved = []
    edges = []

    for f in parsed.features:
        reason = "dependency" if f.is_dependency else "requested"
        node = ScopeNode(
            feature=f.feature,
            label=f.feature,
            description=f.description or "",
            inclusion_reason=reason,
            depends_on=list(f.depends_on),
            priority=f.priority,
        )
        resolved.append(node)
        if not f.is_dependency:
            requested.append(node)

        for dep_slug in f.depends_on:
            edges.append(DependencyEdge(from_feature=f.feature, to_feature=dep_slug))

    return ModelsScope(
        target=target,
        raw_input=parsed.raw_input,
        requested_features=requested,
        resolved_features=resolved,
        dependency_edges=edges,
        known_exclusions=list(parsed.known_exclusions),
        frozen=parsed.frozen,
        scope_hash=parsed.scope_hash,
    )


def _print_report(report: ReconReport) -> None:
    """Print a human-readable summary of the recon run."""
    typer.echo(f"\n{'=' * 50}")
    typer.echo(f"Recon Report: {report.target}")
    typer.echo(f"{'=' * 50}")
    typer.echo(f"Run ID:   {report.run_id}")
    typer.echo(f"Duration: {report.duration_seconds:.1f}s")

    ran = len(report.results)
    skipped = len(report.modules_skipped)
    failed = len(report.modules_failed)
    typer.echo(f"Modules:  {ran} ran, {skipped} skipped, {failed} failed")

    if report.modules_skipped:
        typer.echo(f"  Skipped: {', '.join(report.modules_skipped)}")
    if report.modules_failed:
        typer.echo(f"  Failed:  {', '.join(report.modules_failed)}")

    typer.echo(f"\nFacts gathered: {report.total_facts}")
    if report.facts_by_module:
        typer.echo("  By module:")
        for mod, count in sorted(report.facts_by_module.items(), key=lambda x: -x[1]):
            typer.echo(f"    {mod:20s} {count:>4d}")

    if report.facts_by_feature:
        typer.echo("  By feature:")
        for feat, count in sorted(report.facts_by_feature.items(), key=lambda x: -x[1]):
            typer.echo(f"    {feat:20s} {count:>4d}")

    if report.coverage_gaps:
        typer.echo("\nCoverage gaps (no authoritative facts):")
        for gap in report.coverage_gaps:
            typer.echo(f"  - {gap}")

    if report.errors:
        typer.echo(f"\nErrors ({len(report.errors)}):")
        for err in report.errors:
            typer.echo(f"  [{err.error_type}] {err.message}")

    typer.echo("")


def _normalise_url(url: str) -> str:
    """Ensure a URL has a scheme (defaults to https://)."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


async def _run_recon(
    target: str,
    scope_str: str,
    store_path: str,
    modules_str: str,
    max_concurrent: int,
) -> int:
    """Run the recon pipeline. Returns exit code (0=success, 1=no facts)."""
    parsed = parse_scope(scope_str, target=target) if scope_str else None
    if parsed is not None and not parsed.frozen:
        freeze_scope(parsed)
    models_scope = _bridge_scope(parsed, target)

    store = SpecStore(root=store_path)

    def on_progress(progress: ReconProgress) -> None:
        typer.echo(f"  [{progress.module}] {progress.message}")

    artifact_dir = str(Path(store_path) / ".specstore" / "artifacts")
    Path(artifact_dir).mkdir(parents=True, exist_ok=True)

    orchestrator = ReconOrchestrator(
        spec_store=store,
        keychain=keychain_module,
        artifact_dir=artifact_dir,
        progress_callback=on_progress,
    )

    module_filter = [m.strip() for m in modules_str.split(",") if m.strip()] or None

    report = await orchestrator.run(
        target=target,
        scope=models_scope,
        modules=module_filter,
        max_concurrent=max_concurrent,
    )

    _print_report(report)
    return 0 if report.total_facts > 0 else 1


@app.command()
def recon(
    target: str = typer.Argument(..., help="Target SaaS URL or domain"),
    scope: str = typer.Option("", "--scope", help="Comma-separated list of features to recon"),
    store: str = typer.Option(".", "--store", help="Root directory for .specstore"),
    modules: str = typer.Option("", "--modules", help="Comma-separated module names (default: all)"),  # noqa: E501
    max_concurrent: int = typer.Option(3, "--max-concurrent", help="Max parallel modules"),
) -> None:
    """Gather intelligence from all available sources for a target SaaS."""
    if max_concurrent < 1:
        typer.echo("Error: --max-concurrent must be >= 1", err=True)
        raise typer.Exit(code=2)
    exit_code = asyncio.run(_run_recon(target, scope, store, modules, max_concurrent))
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@app.command()
def duplicate(
    target: str = typer.Argument(..., help="Target SaaS URL or domain"),
    output: str = typer.Option(..., "--output", help="Output repo (owner/repo)"),
    scope: str = typer.Option("", "--scope", help="Comma-separated list of features to duplicate"),
    max_iterations: int = typer.Option(10, "--max-iterations", help="Max convergence iterations"),
    target_parity: float = typer.Option(
        95.0, "--target-parity", help="Stop convergence when parity >= this"
    ),
    clone_url: str = typer.Option(
        "http://localhost:3000", "--clone-url", help="URL of the clone under test"
    ),
    use_multi_ai: bool = typer.Option(
        True, "--use-multi-ai/--no-multi-ai", help="Use multi-AI synthesis"
    ),
    cw_home: str = typer.Option(
        str(Path.home() / ".chief-wiggum"), "--cw-home", help="Path to chief-wiggum home"
    ),
) -> None:
    """Full pipeline: recon → spec → build → compare → loop."""

    config = DuplicateConfig(
        target_url=target,
        output_repo=output,
        scope_str=scope or "all",
        max_iterations=max_iterations,
        target_parity=target_parity,
        clone_url=clone_url,
        use_multi_ai=use_multi_ai,
    )

    pipeline = DuplicatePipeline(cw_home=cw_home, work_dir=Path("."))

    async def _run():
        report = await pipeline.run(config)
        typer.echo(report.format_summary())
        if report.errors:
            raise typer.Exit(code=1)

    asyncio.run(_run())


@app.command()
def compare(
    target: str = typer.Argument(..., help="Target SaaS URL or domain"),
    clone_url: str = typer.Option(
        "http://localhost:3000", "--clone-url", help="URL of the clone under test"
    ),
    suite_dir: str = typer.Option(".", "--suite-dir", help="Root directory for conformance tests"),
    scope: str = typer.Option("", "--scope", help="Comma-separated list of features to compare"),
    min_parity: float = typer.Option(
        0.0, "--min-parity", help="Exit 1 if parity score falls below this threshold"
    ),
) -> None:
    """Compare clone against target for behavioral conformance."""

    target_url = _normalise_url(target)
    clone_url = _normalise_url(clone_url)

    async def _run() -> float:
        # Only filter by scope if explicitly provided
        models_scope = None
        if scope:
            parsed = parse_scope(scope, target=target)
            if not parsed.frozen:
                freeze_scope(parsed)
            models_scope = _bridge_scope(parsed, target)

        comparator = BehavioralComparator(Path(suite_dir))
        result = await comparator.compare(target_url, clone_url, scope=models_scope)
        typer.echo(format_report(result))
        return result.parity_score

    parity = asyncio.run(_run())
    if parity < min_parity:
        raise typer.Exit(code=1)


@app.command()
def converge(
    target: str = typer.Argument(..., help="Target SaaS URL or domain"),
    output: str = typer.Option(..., "--output", help="Output repo (owner/repo)"),
    clone_url: str = typer.Option(
        "http://localhost:3000", "--clone-url", help="URL of the clone under test"
    ),
    suite_dir: str = typer.Option(".", "--suite-dir", help="Root directory for conformance tests"),
    scope: str = typer.Option("", "--scope", help="Comma-separated list of features to converge"),
    max_iterations: int = typer.Option(10, "--max-iterations", help="Max convergence iterations"),
    target_parity: float = typer.Option(
        95.0, "--target-parity", help="Stop convergence when parity >= this"
    ),
) -> None:
    """Run gap analysis and feed back into build pipeline."""

    target_url = _normalise_url(target)
    clone_url = _normalise_url(clone_url)

    async def _run():
        parsed_scope = parse_scope(scope, target=target) if scope else None
        if parsed_scope is None:
            parsed_scope = ParsedScope(target=target, raw_input="all")
        if not parsed_scope.frozen:
            freeze_scope(parsed_scope)

        store = SpecStore(root=suite_dir)
        comparator = BehavioralComparator(Path(suite_dir))
        history_dir = Path(suite_dir) / "convergence_history"
        gap_analyzer = GapAnalyzer(store, history_dir)

        orchestrator = ConvergenceOrchestrator(
            spec_store=store,
            comparator=comparator,
            gap_analyzer=gap_analyzer,
        )

        config = ConvergenceConfig(
            target_url=target_url,
            clone_url=clone_url,
            scope=parsed_scope,
            max_iterations=max_iterations,
            target_parity=target_parity,
            repo=output,
            history_dir=history_dir,
        )

        report = await orchestrator.run(config)
        typer.echo(report.format_summary())
        return report

    report = asyncio.run(_run())
    # Exit 0 if parity achieved or final parity meets target
    if report.stop_reason != "parity_achieved" and report.final_parity < target_parity:
        raise typer.Exit(code=1)


@app.command()
def analyze(
    store: str = typer.Option(".", "--store", help="Root directory for .specstore"),
) -> None:
    """Analyze and curate gathered facts — filter noise, dedup, cluster."""
    from scripts.fact_analyzer import FactAnalyzer
    from scripts.spec_store import SpecStore

    spec_store = SpecStore(root=store)
    analyzer = FactAnalyzer(spec_store)

    # Load all active facts from the store
    index = spec_store._load_index()
    all_facts = []
    for fact_id, meta in index["facts"].items():
        if not meta.get("deleted_at") and not meta.get("superseded_by"):
            try:
                all_facts.append(spec_store.get_fact(fact_id))
            except Exception:  # noqa: BLE001
                pass

    if not all_facts:
        typer.echo("No active facts found in store.")
        return

    report = analyzer.analyze_report(all_facts)

    typer.echo(f"\n{'=' * 50}")
    typer.echo("Fact Analysis Report")
    typer.echo(f"{'=' * 50}")
    typer.echo(f"Total facts:     {report.total_facts}")
    typer.echo(f"Noise filtered:  {report.noise_filtered}")
    typer.echo(f"Deduplicated:    {report.deduplicated}")
    typer.echo(f"Kept:            {report.kept}")

    if report.noise_patterns:
        typer.echo("\nNoise breakdown:")
        for rule, count in sorted(report.noise_patterns.items(), key=lambda x: -x[1]):
            typer.echo(f"  {rule:30s} {count:>5d}")

    if report.facts_by_feature:
        typer.echo("\nFacts by feature (kept):")
        for feat, count in sorted(report.facts_by_feature.items(), key=lambda x: -x[1]):
            typer.echo(f"  {feat:30s} {count:>5d}")

    if report.clusters:
        typer.echo(f"\nSub-feature clusters ({len(report.clusters)}):")
        for cluster, ids in sorted(report.clusters.items()):
            typer.echo(f"  {cluster:40s} {len(ids):>4d} facts")

    typer.echo("")


@app.command()
def model(
    target: str = typer.Argument(..., help="Target SaaS URL or domain"),
    scope: str = typer.Option("", "--scope", help="Comma-separated feature names to include"),
    store: str = typer.Option(".", "--store", help="Root directory for .specstore (source of facts)"),
    output_dir: str = typer.Option(".", "--output-dir", help="Directory to write model_v*.json snapshots"),
    max_iterations: int = typer.Option(5, "--max-iterations", help="Max experiment-refine iterations"),
    max_experiments: int = typer.Option(50, "--max-experiments", help="Max experiments per iteration"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Build model from facts only, skip experiments"),
) -> None:
    """Build domain model via scientific recon loop (observe → hypothesize → experiment → refine)."""
    import json as _json

    from scripts.fact_analyzer import FactAnalyzer
    from scripts.hypothesis_builder import HypothesisBuilder
    from scripts.scientific_recon import ScientificRecon
    from scripts.spec_store import SpecStore

    target_url = _normalise_url(target)
    spec_store = SpecStore(root=store)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Load and curate facts from the store
    analyzer = FactAnalyzer(spec_store)
    index = spec_store._load_index()
    raw_facts = []
    for fact_id, meta in index["facts"].items():
        if not meta.get("deleted_at") and not meta.get("superseded_by"):
            try:
                raw_facts.append(spec_store.get_fact(fact_id))
            except Exception:  # noqa: BLE001
                pass

    scope_list = [s.strip() for s in scope.split(",") if s.strip()] if scope else []
    if scope_list:
        raw_facts = [f for f in raw_facts if f.feature in scope_list]

    curated_facts = asyncio.run(analyzer.analyze(raw_facts)) if raw_facts else []

    typer.echo(f"Facts loaded: {len(raw_facts)} raw, {len(curated_facts)} curated")

    if dry_run or not curated_facts:
        # Build model from facts only — no experiments
        dm = HypothesisBuilder().build(curated_facts or raw_facts, target_url)
        dm.iteration = 0
        snapshot = out / "model_v0.json"
        dm.save(snapshot)
        typer.echo(f"\nDomain model (dry run):")
        typer.echo(f"  Entities:     {len(dm.entities)}")
        typer.echo(f"  Hypotheses:   {dm.total_hypotheses()}")
        typer.echo(f"  Confidence:   {dm.overall_confidence():.0%}")
        typer.echo(f"  Saved to:     {snapshot}")
        return

    recon = ScientificRecon(
        target_url=target_url,
        output_dir=out,
    )

    dm = asyncio.run(
        recon.run(
            curated_facts,
            max_iterations=max_iterations,
            max_experiments=max_experiments,
        )
    )

    # Final snapshot
    final_path = out / "model_final.json"
    dm.save(final_path)

    typer.echo(f"\nDomain model (final):")
    typer.echo(f"  Entities:     {len(dm.entities)}")
    typer.echo(f"  Hypotheses:   {dm.total_hypotheses()}")
    typer.echo(f"  Validated:    {dm.validated_hypotheses()}")
    typer.echo(f"  Confidence:   {dm.overall_confidence():.0%}")
    typer.echo(f"  Iterations:   {dm.iteration}")
    typer.echo(f"  Saved to:     {final_path}")

    if dm.entities:
        typer.echo("\nEntities discovered:")
        for name, entity in dm.entities.items():
            typer.echo(
                f"  {name:20s}  ops={len(entity.operations)}  "
                f"fields={len(entity.fields)}  "
                f"confidence={entity.confidence:.0%}"
            )


@app.command("generate-tickets")
def generate_tickets(
    model_path: str = typer.Argument(..., help="Path to domain model JSON"),
    repo: str = typer.Option("", "--repo", help="GitHub repo to create issues in (owner/repo)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print tickets without creating issues"),
) -> None:
    """Generate granular implementation tickets from a domain model."""
    import subprocess

    from scripts.domain_model import DomainModel
    from scripts.model_ticket_generator import ModelTicketGenerator

    model_file = Path(model_path)
    if not model_file.exists():
        typer.echo(f"Error: model file not found: {model_path}", err=True)
        raise typer.Exit(1)

    dm = DomainModel.load(model_file)
    generator = ModelTicketGenerator()
    tickets = generator.generate_tickets(dm)

    typer.echo(f"\nGenerated {len(tickets)} tickets from {len(dm.entities)} entities")
    typer.echo(f"  Priority 1 (model):       {sum(1 for t in tickets if t.priority == 1)}")
    typer.echo(f"  Priority 2 (CRUD):        {sum(1 for t in tickets if t.priority == 2)}")
    typer.echo(f"  Priority 3 (transitions): {sum(1 for t in tickets if t.priority == 3)}")
    typer.echo(f"  Priority 4 (UI):          {sum(1 for t in tickets if t.priority == 4)}")
    typer.echo(f"  Priority 5 (polish):      {sum(1 for t in tickets if t.priority == 5)}")

    if dry_run:
        typer.echo("\nTickets (dry run):")
        for ticket in sorted(tickets, key=lambda t: (t.priority, t.entity, t.id)):
            typer.echo(f"  [{ticket.priority}] #{ticket.id}: {ticket.title}")
        return

    if not repo:
        typer.echo("\nNo --repo specified. Use --dry-run to preview or --repo owner/repo to create issues.")
        return

    typer.echo(f"\nCreating GitHub issues in {repo} ...")
    created = 0
    failed = 0
    for ticket in sorted(tickets, key=lambda t: (t.priority, t.entity, t.id)):
        body = generator.render_issue_body(ticket)
        labels = ",".join(ticket.labels) if ticket.labels else ""
        cmd = [
            "gh", "issue", "create",
            "--repo", repo,
            "--title", ticket.title,
            "--body", body,
        ]
        if labels:
            cmd += ["--label", labels]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            typer.echo(f"  Created: {ticket.title}")
            created += 1
        else:
            typer.echo(f"  Failed:  {ticket.title} — {result.stderr.strip()}", err=True)
            failed += 1

    typer.echo(f"\nDone: {created} created, {failed} failed")
    if failed > 0:
        raise typer.Exit(1)


@secrets_app.command("list")
def secrets_list(
    service: str = typer.Option(DEFAULT_SERVICE, "--service", help="Keyring service namespace"),
) -> None:
    """List known secrets and whether they are stored (never shows values)."""
    entries = list_secrets(service=service)
    if not entries:
        all_services = ", ".join(KNOWN_KEYS.keys())
        typer.echo(f"No known keys for service '{service}'. Known services: {all_services}")
        return
    typer.echo(f"=== Keyring (service: {service}) ===")
    for entry in entries:
        status = "[stored]  " if entry["stored"] else "[not set] "
        typer.echo(f"  {status}  {entry['name']}")


@secrets_app.command("set")
def secrets_set(
    name: str = typer.Argument(..., help="Secret key name"),
    value: str = typer.Option("", "--value", help="Secret value (alternative to interactive prompt)"),  # noqa: E501
    service: str = typer.Option(DEFAULT_SERVICE, "--service", help="Keyring service namespace"),
) -> None:
    """Store a secret in the system keyring (prompts for value securely)."""
    if not value:
        value = getpass.getpass(f"Enter value for {name}: ")
    if not value:
        typer.echo("Error: empty value", err=True)
        raise typer.Exit(1)
    set_secret(name, value, service=service)
    typer.echo(f"Stored '{name}' in keyring (service: {service})")


@secrets_app.command("delete")
def secrets_delete(
    name: str = typer.Argument(..., help="Secret key name"),
    service: str = typer.Option(DEFAULT_SERVICE, "--service", help="Keyring service namespace"),
) -> None:
    """Remove a secret from the system keyring."""
    if not has_secret(name, service=service):
        typer.echo(f"'{name}' not found in keyring (service: {service})", err=True)
        raise typer.Exit(1)
    delete_secret(name, service=service)
    typer.echo(f"Deleted '{name}' from keyring (service: {service})")


if __name__ == "__main__":
    app()
