"""
Tests for Settings.oauth_redirect_uri.

The redirect URI is what the provider matches against its own client
registration, so an empty one is not a harmless default — it is a failed
authorization with a message that points at the provider's portal rather than
at the .env file that caused it.
"""

import warnings

import pytest

from app.config import Settings
from app.schemas.enums import ProviderName


def _settings(**overrides) -> Settings:
    return Settings(api_base_url="https://example.test", **overrides)


class TestDerivedFromApiBaseUrl:
    def test_derives_the_callback_from_api_base_url(self) -> None:
        uri = _settings().oauth_redirect_uri(ProviderName.SUUNTO)

        assert uri == "https://example.test/api/v1/oauth/suunto/callback"

    def test_each_provider_gets_its_own_path(self) -> None:
        settings = _settings()

        assert settings.oauth_redirect_uri(ProviderName.POLAR).endswith("/oauth/polar/callback")
        assert settings.oauth_redirect_uri(ProviderName.SUUNTO).endswith("/oauth/suunto/callback")


class TestLegacyOverride:
    def test_a_real_legacy_value_still_wins(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            uri = _settings(suunto_redirect_uri="https://legacy.test/cb").oauth_redirect_uri(ProviderName.SUUNTO)

        assert uri == "https://legacy.test/cb"

    def test_a_real_legacy_value_is_deprecated(self) -> None:
        with pytest.warns(DeprecationWarning):
            _settings(suunto_redirect_uri="https://legacy.test/cb").oauth_redirect_uri(ProviderName.SUUNTO)

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_a_blank_legacy_value_falls_through(self, blank: str) -> None:
        """`SUUNTO_REDIRECT_URI=` in an .env is an unset variable, not an empty
        redirect URI.

        Returning "" here put `redirect_uri=` in the authorize URL, and Suunto
        answered "At least one redirect_uri must be registered with the client"
        — an error that sends you to the developer portal for a problem that
        lives in your own config file.
        """
        uri = _settings(suunto_redirect_uri=blank).oauth_redirect_uri(ProviderName.SUUNTO)

        assert uri == "https://example.test/api/v1/oauth/suunto/callback"

    def test_a_blank_legacy_value_does_not_warn(self) -> None:
        """Nothing was overridden, so there is nothing to deprecate."""
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            _settings(suunto_redirect_uri="").oauth_redirect_uri(ProviderName.SUUNTO)
