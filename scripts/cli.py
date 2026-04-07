"""Duplicat-Rex CLI — entry point."""

import asyncio
import getpass
from typing import Any

import typer

import scripts.keychain as keychain_module
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
from scripts.recon.orchestrator import ReconOrchestrator, ReconReport
from scripts.scope import parse_scope
from scripts.spec_store import SpecStore

app = typer.Typer(
    name="duplicat-rex",
    help="Agentic SaaS reverse-engineering engine.",
    no_args_is_help=True,
)

secrets_app = typer.Typer(help="Manage credentials stored in the system keyring.")
app.add_typer(secrets_app, name="secrets")


def _bridge_scope(
    parsed: Any,
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


async def _run_recon(
    target: str,
    scope_str: str,
    store_path: str,
    modules_str: str,
    max_concurrent: int,
) -> int:
    """Run the recon pipeline. Returns exit code (0=success, 1=no facts)."""
    parsed = parse_scope(scope_str, target=target) if scope_str else None
    models_scope = _bridge_scope(parsed, target)

    store = SpecStore(root=store_path)

    def on_progress(progress: Any) -> None:
        typer.echo(f"  [{progress.module}] {progress.message}")

    orchestrator = ReconOrchestrator(
        spec_store=store,
        keychain=keychain_module,
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
    exit_code = asyncio.run(_run_recon(target, scope, store, modules, max_concurrent))
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@app.command()
def duplicate(
    target: str = typer.Argument(..., help="Target SaaS URL or domain"),
    output: str = typer.Option(..., "--output", help="Output repo (owner/repo)"),
    scope: str = typer.Option("", "--scope", help="Comma-separated list of features to duplicate"),
) -> None:
    """Full pipeline: recon → spec → build → compare → loop."""
    typer.echo(f"Duplicate: {target} → {output} (scope: {scope or 'all'})")


@app.command()
def compare(
    clone: str = typer.Argument(..., help="Clone repo (owner/repo)"),
    target: str = typer.Option(..., "--target", help="Target SaaS URL or domain"),
) -> None:
    """Compare clone against target for behavioral conformance."""
    typer.echo(f"Compare: {clone} vs {target}")


@app.command()
def converge(
    clone: str = typer.Argument(..., help="Clone repo (owner/repo)"),
    target: str = typer.Option(..., "--target", help="Target SaaS URL or domain"),
) -> None:
    """Run gap analysis and feed back into build pipeline."""
    typer.echo(f"Converge: {clone} → {target}")


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
    service: str = typer.Option(DEFAULT_SERVICE, "--service", help="Keyring service namespace"),
) -> None:
    """Store a secret in the system keyring (prompts for value securely)."""
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
