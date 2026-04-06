"""Tests for keychain secret management (ticket #5).

Uses a dedicated test service namespace ("duplicat-rex-test") to avoid
touching real secrets stored under "duplicat-rex" or "chief-wiggum".

INV-028: Secrets must never appear in logs, progress messages, or return values
         of non-get functions.
INV-029: Modules only access credentials they declared.
INV-030: Secrets never in env vars.
"""

from scripts.keychain import (
    DEFAULT_SERVICE,
    KNOWN_KEYS,
    delete_secret,
    get_secret,
    has_secret,
    list_secrets,
    set_secret,
)

TEST_SERVICE = "duplicat-rex-test"
TEST_KEY = "TEST_CREDENTIAL"
TEST_VALUE = "s3cr3t-v4lue"


class TestStoreRetrieveDeleteCycle:
    """Core store / retrieve / delete round-trip."""

    def setup_method(self) -> None:
        # Ensure a clean slate before each test.
        delete_secret(TEST_KEY, service=TEST_SERVICE)

    def teardown_method(self) -> None:
        # Clean up after each test.
        delete_secret(TEST_KEY, service=TEST_SERVICE)

    def test_secret_not_present_initially(self) -> None:
        assert get_secret(TEST_KEY, service=TEST_SERVICE) is None

    def test_has_secret_returns_false_when_absent(self) -> None:
        assert has_secret(TEST_KEY, service=TEST_SERVICE) is False

    def test_set_then_get_returns_value(self) -> None:
        set_secret(TEST_KEY, TEST_VALUE, service=TEST_SERVICE)
        result = get_secret(TEST_KEY, service=TEST_SERVICE)
        assert result == TEST_VALUE

    def test_has_secret_returns_true_after_set(self) -> None:
        set_secret(TEST_KEY, TEST_VALUE, service=TEST_SERVICE)
        assert has_secret(TEST_KEY, service=TEST_SERVICE) is True

    def test_delete_removes_secret(self) -> None:
        set_secret(TEST_KEY, TEST_VALUE, service=TEST_SERVICE)
        deleted = delete_secret(TEST_KEY, service=TEST_SERVICE)
        assert deleted is True
        assert get_secret(TEST_KEY, service=TEST_SERVICE) is None

    def test_delete_returns_false_when_absent(self) -> None:
        result = delete_secret(TEST_KEY, service=TEST_SERVICE)
        assert result is False

    def test_overwrite_existing_secret(self) -> None:
        set_secret(TEST_KEY, "original", service=TEST_SERVICE)
        set_secret(TEST_KEY, "updated", service=TEST_SERVICE)
        assert get_secret(TEST_KEY, service=TEST_SERVICE) == "updated"


class TestListSecrets:
    """list_secrets returns key names and status but never values."""

    def test_list_returns_list(self) -> None:
        result = list_secrets(service=DEFAULT_SERVICE)
        assert isinstance(result, list)

    def test_list_entries_have_required_fields(self) -> None:
        result = list_secrets(service=DEFAULT_SERVICE)
        for entry in result:
            assert "name" in entry
            assert "service" in entry
            assert "stored" in entry

    def test_list_entries_have_no_value_field(self) -> None:
        """INV-028: values must never appear in list output."""
        result = list_secrets(service=DEFAULT_SERVICE)
        for entry in result:
            assert "value" not in entry
            assert "password" not in entry
            assert "secret" not in entry

    def test_list_stored_is_boolean(self) -> None:
        result = list_secrets(service=DEFAULT_SERVICE)
        for entry in result:
            assert isinstance(entry["stored"], bool)

    def test_list_unknown_service_returns_empty(self) -> None:
        result = list_secrets(service="nonexistent-service-xyz")
        assert result == []

    def test_list_chief_wiggum_service(self) -> None:
        result = list_secrets(service="chief-wiggum")
        names = [e["name"] for e in result]
        assert "ANTHROPIC_API_KEY" in names
        assert "OPENAI_API_KEY" in names
        assert "GEMINI_API_KEY" in names

    def test_list_duplicat_rex_service(self) -> None:
        result = list_secrets(service="duplicat-rex")
        names = [e["name"] for e in result]
        assert any("trello-com" in n for n in names)


class TestServiceNamespaceIsolation:
    """Secrets in different namespaces must not bleed into each other."""

    NAMESPACE_A = "duplicat-rex-test-a"
    NAMESPACE_B = "duplicat-rex-test-b"
    KEY = "SHARED_KEY_NAME"

    def setup_method(self) -> None:
        delete_secret(self.KEY, service=self.NAMESPACE_A)
        delete_secret(self.KEY, service=self.NAMESPACE_B)

    def teardown_method(self) -> None:
        delete_secret(self.KEY, service=self.NAMESPACE_A)
        delete_secret(self.KEY, service=self.NAMESPACE_B)

    def test_set_in_a_does_not_appear_in_b(self) -> None:
        set_secret(self.KEY, "value-a", service=self.NAMESPACE_A)
        assert get_secret(self.KEY, service=self.NAMESPACE_B) is None

    def test_different_values_per_namespace(self) -> None:
        set_secret(self.KEY, "value-a", service=self.NAMESPACE_A)
        set_secret(self.KEY, "value-b", service=self.NAMESPACE_B)
        assert get_secret(self.KEY, service=self.NAMESPACE_A) == "value-a"
        assert get_secret(self.KEY, service=self.NAMESPACE_B) == "value-b"

    def test_delete_in_a_does_not_affect_b(self) -> None:
        set_secret(self.KEY, "value-a", service=self.NAMESPACE_A)
        set_secret(self.KEY, "value-b", service=self.NAMESPACE_B)
        delete_secret(self.KEY, service=self.NAMESPACE_A)
        assert get_secret(self.KEY, service=self.NAMESPACE_B) == "value-b"


class TestInvariants:
    """Verify key architectural invariants."""

    def test_inv_030_default_service_not_in_env(self) -> None:
        """INV-030: DEFAULT_SERVICE must not match any env var prefix."""
        import os

        for key in os.environ:
            # Ensure no env var starts with our service name (would indicate leakage)
            assert not key.startswith("DUPLICAT_REX_SECRET_"), (
                f"Secret leaked into env var: {key}"
            )

    def test_known_keys_exist_for_both_services(self) -> None:
        assert "duplicat-rex" in KNOWN_KEYS
        assert "chief-wiggum" in KNOWN_KEYS

    def test_known_keys_duplicat_rex_follow_naming_convention(self) -> None:
        """Keys must follow duplicat-rex.{category}.{target}.{key-name} pattern."""
        for key in KNOWN_KEYS["duplicat-rex"]:
            parts = key.split(".")
            assert len(parts) == 4, f"Key '{key}' does not follow 4-part convention"
            assert parts[0] == "duplicat-rex"

    def test_get_secret_returns_none_not_raises_on_missing(self) -> None:
        result = get_secret("DEFINITELY_NONEXISTENT_KEY_XYZ", service=TEST_SERVICE)
        assert result is None

    def test_has_secret_returns_false_not_raises_on_missing(self) -> None:
        result = has_secret("DEFINITELY_NONEXISTENT_KEY_XYZ", service=TEST_SERVICE)
        assert result is False
