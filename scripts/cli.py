"""Duplicat-Rex CLI — entry point."""

import getpass

import typer

from scripts.keychain import (
    DEFAULT_SERVICE,
    KNOWN_KEYS,
    delete_secret,
    has_secret,
    list_secrets,
    set_secret,
)

app = typer.Typer(
    name="duplicat-rex",
    help="Agentic SaaS reverse-engineering engine.",
    no_args_is_help=True,
)

secrets_app = typer.Typer(help="Manage credentials stored in the system keyring.")
app.add_typer(secrets_app, name="secrets")


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
