"""Duplicat-Rex CLI — entry point."""

import typer

app = typer.Typer(
    name="duplicat-rex",
    help="Agentic SaaS reverse-engineering engine.",
    no_args_is_help=True,
)


@app.command()
def recon(
    target: str = typer.Argument(..., help="Target SaaS URL or domain"),
    scope: str = typer.Option("", "--scope", help="Comma-separated list of features to recon"),
) -> None:
    """Gather intelligence from all available sources for a target SaaS."""
    typer.echo(f"Recon: {target} (scope: {scope or 'all'})")


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


if __name__ == "__main__":
    app()
