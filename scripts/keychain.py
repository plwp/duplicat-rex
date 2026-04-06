"""
Keychain secret management for duplicat-rex.

Uses the `keyring` library for secure cross-platform secret storage.
On macOS this uses Keychain, on Linux it uses SecretService/KWallet.

Secrets are fetched on demand and passed directly to API constructors.
They are NEVER set as environment variables, NEVER printed, and NEVER logged.

Naming convention:
    duplicat-rex.{category}.{target-or-service}.{key-name}

Examples:
    duplicat-rex.target.trello-com.username
    duplicat-rex.target.trello-com.password
    duplicat-rex.target.trello-com.api-key
    duplicat-rex.target.trello-com.api-token

AI keys are shared with chief-wiggum via its service namespace:
    chief-wiggum.ANTHROPIC_API_KEY

As a module:
    from scripts.keychain import get_secret, set_secret, delete_secret, list_secrets
    api_key = get_secret("ANTHROPIC_API_KEY", service="chief-wiggum")

INV-028: Secrets never enter Facts, SpecBundles, logs, progress messages, or LLM prompts.
INV-029: Modules only access credentials they declared.
INV-030: Secrets never in env vars.
"""

import sys

try:
    import keyring
    import keyring.errors
except ImportError:
    print(
        "Missing dependency: keyring\n"
        "Install with: pip install keyring",
        file=sys.stderr,
    )
    sys.exit(1)

DEFAULT_SERVICE = "duplicat-rex"

# Known keys under the duplicat-rex service namespace.
# AI keys live under the chief-wiggum service — use get_secret(..., service="chief-wiggum").
KNOWN_KEYS: dict[str, list[str]] = {
    "duplicat-rex": [
        "duplicat-rex.target.trello-com.username",
        "duplicat-rex.target.trello-com.password",
        "duplicat-rex.target.trello-com.api-key",
        "duplicat-rex.target.trello-com.api-token",
    ],
    "chief-wiggum": [
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
    ],
}


def get_secret(key: str, service: str = DEFAULT_SERVICE) -> str | None:
    """Fetch a secret from the system keyring. Returns None if not found."""
    try:
        return keyring.get_password(service, key)
    except Exception:
        return None


def has_secret(key: str, service: str = DEFAULT_SERVICE) -> bool:
    """Check if a secret exists in the keyring."""
    try:
        return keyring.get_password(service, key) is not None
    except Exception:
        return False


def set_secret(key: str, value: str, service: str = DEFAULT_SERVICE) -> None:
    """Store a secret in the system keyring. Overwrites if it exists."""
    keyring.set_password(service, key, value)


def delete_secret(key: str, service: str = DEFAULT_SERVICE) -> bool:
    """Delete a secret from the keyring. Returns True if it existed."""
    try:
        keyring.delete_password(service, key)
        return True
    except keyring.errors.PasswordDeleteError:
        return False


def list_secrets(service: str = DEFAULT_SERVICE) -> list[dict[str, object]]:
    """Return status of known keys for the given service (never the values)."""
    keys = KNOWN_KEYS.get(service, [])
    return [
        {"name": k, "service": service, "stored": has_secret(k, service)}
        for k in keys
    ]
