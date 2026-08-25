from logging import getLogger

import httpx

from app.config import settings
from app.schemas.enums import ProviderName
from app.schemas.model_crud.credentials import (
    OAuthTokenResponse,
    ProviderCredentials,
    ProviderEndpoints,
)
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.utils.structured_logging import log_structured

logger = getLogger(__name__)


class PolarOAuth(BaseOAuthTemplate):
    """Polar OAuth 2.0 implementation."""

    @property
    def endpoints(self) -> ProviderEndpoints:
        return ProviderEndpoints(
            authorize_url="https://flow.polar.com/oauth2/authorization",
            token_url="https://polarremote.com/v2/oauth2/token",
        )

    @property
    def credentials(self) -> ProviderCredentials:
        return ProviderCredentials(
            client_id=settings.polar_client_id or "",
            client_secret=(settings.polar_client_secret.get_secret_value() if settings.polar_client_secret else ""),
            redirect_uri=settings.oauth_redirect_uri(ProviderName.POLAR),
            default_scope=settings.polar_default_scope,
        )

    def _get_provider_user_info(self, token_response: OAuthTokenResponse, user_id: str) -> dict[str, str | None]:
        """Extracts Polar user ID from token response and registers user."""
        raw = token_response.model_extra.get("x_user_id") if token_response.model_extra else None
        provider_user_id = str(raw) if raw is not None else None

        if provider_user_id:
            self._register_user(token_response.access_token, user_id)

        return {"user_id": provider_user_id, "username": None}

    def _register_user(self, access_token: str, member_id: str) -> None:
        """Registers the user with Polar API.

        Registration is not a formality. Polar returns only exercises that were
        uploaded to Flow *after* the user was registered with the client, so a
        registration that fails leaves a connection which authorises fine, syncs
        without error, and never yields a single exercise -- indistinguishable
        from an athlete who simply has not trained. Swallowing the outcome made
        that state undiagnosable, which is why every path now says what happened.

        Still non-fatal: the token exchange already succeeded, and discarding a
        working connection over one call that can be retried would be worse than
        recording the failure.
        """
        register_url = f"{self.api_base_url}/v3/users"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            response = httpx.post(register_url, json={"member-id": member_id}, headers=headers, timeout=10.0)
        except httpx.HTTPError as e:
            log_structured(
                logger,
                "error",
                "Polar user registration request failed - exercises will not be delivered",
                provider=ProviderName.POLAR,
                member_id=member_id,
                error=str(e),
            )
            return

        # 409 is the ordinary reconnect path: Polar already knows this member-id.
        if response.status_code == httpx.codes.CONFLICT:
            log_structured(
                logger,
                "info",
                "Polar user already registered",
                provider=ProviderName.POLAR,
                member_id=member_id,
            )
        elif response.is_success:
            log_structured(
                logger,
                "info",
                "Registered user with Polar",
                provider=ProviderName.POLAR,
                member_id=member_id,
                status_code=response.status_code,
            )
        else:
            log_structured(
                logger,
                "error",
                "Polar user registration rejected - exercises will not be delivered",
                provider=ProviderName.POLAR,
                member_id=member_id,
                status_code=response.status_code,
                response_body=response.text[:500],
            )

    def deregister_user(self, access_token: str, provider_user_id: str | None = None) -> None:
        """Call Polar's user deregistration endpoint to remove the app association."""

        if not provider_user_id:
            raise ValueError("Polar deregistration requires provider_user_id")

        deregister_url = f"{self.api_base_url}/v3/users/{provider_user_id}"
        headers = {
            "Authorization": f"Bearer {access_token}",
        }
        response = httpx.delete(deregister_url, headers=headers, timeout=10.0)
        response.raise_for_status()
