# IMPORTANT: LEGACY V0 CODE - Deprecated since version 1.0.0, scheduled for removal April 1, 2026
# This file is part of the legacy (V0) implementation of OpenHands and will be removed soon as we complete the migration to V1.
# OpenHands V1 uses the Software Agent SDK for the agentic core and runs a new application server. Please refer to:
#   - V1 agentic core (SDK): https://github.com/OpenHands/software-agent-sdk
#   - V1 application server (in this repo): openhands/app_server/
# Unless you are working on deprecation, please avoid extending this legacy file and consult the V1 codepaths above.
# Tag: Legacy-V0
# This module belongs to the old V0 web server. The V1 application server lives under openhands/app_server/.
from dataclasses import dataclass

from fastapi import Request
from pydantic import SecretStr

from openhands.app_server import shared
from openhands.app_server.integrations.provider import PROVIDER_TOKEN_TYPE
from openhands.app_server.secrets.secrets_models import Secrets
from openhands.app_server.secrets.secrets_store import SecretsStore
from openhands.app_server.settings.settings_models import Settings
from openhands.app_server.settings.settings_store import SettingsStore
from openhands.app_server.user_auth.user_auth import UserAuth


@dataclass
class DefaultUserAuth(UserAuth):
    """Default user authentication mechanism"""

    # OH multi-user patch: user id injected from the X-User-Id request
    # header by the reverse proxy (user_proxy.py). None when absent.
    _user_id: str | None = None
    _settings: Settings | None = None
    _settings_store: SettingsStore | None = None
    _secrets_store: SecretsStore | None = None
    _secrets: Secrets | None = None

    async def get_user_id(self) -> str | None:
        """OH multi-user patch: return the user id from X-User-Id (set by the
        reverse proxy). None when absent = single-user / unauthenticated."""
        return self._user_id

    async def get_user_email(self) -> str | None:
        """The default implementation does not support multi tenancy, so email is always None"""
        return None

    async def get_access_token(self) -> SecretStr | None:
        """The default implementation does not support multi tenancy, so access_token is always None"""
        return None

    async def get_user_settings_store(self) -> SettingsStore:
        settings_store = self._settings_store
        if settings_store:
            return settings_store
        user_id = await self.get_user_id()
        settings_store = await shared.SettingsStoreImpl.get_instance(user_id)
        if settings_store is None:
            raise ValueError('Failed to get settings store instance')
        self._settings_store = settings_store
        return settings_store

    async def get_user_settings(self) -> Settings | None:
        settings = self._settings
        if settings:
            return settings
        settings_store = await self.get_user_settings_store()
        settings = await settings_store.load()
        self._settings = settings
        return settings

    async def get_secrets_store(self) -> SecretsStore:
        secrets_store = self._secrets_store
        if secrets_store:
            return secrets_store
        user_id = await self.get_user_id()
        secret_store = await shared.SecretsStoreImpl.get_instance(user_id)
        if secret_store is None:
            raise ValueError('Failed to get secrets store instance')
        self._secrets_store = secret_store
        return secret_store

    async def get_secrets(self) -> Secrets | None:
        user_secrets = self._secrets
        if user_secrets:
            return user_secrets
        secrets_store = await self.get_secrets_store()
        user_secrets = await secrets_store.load()
        self._secrets = user_secrets
        return user_secrets

    async def get_provider_tokens(self) -> PROVIDER_TOKEN_TYPE | None:
        user_secrets = await self.get_secrets()
        if user_secrets is None:
            return None
        return user_secrets.provider_tokens

    async def get_mcp_api_key(self) -> str | None:
        return None

    @classmethod
    async def get_instance(cls, request: Request) -> UserAuth:
        # OH multi-user patch: read the user id injected by the reverse proxy.
        uid = request.headers.get('X-User-Id')
        return DefaultUserAuth(_user_id=uid)

    @classmethod
    async def get_for_user(cls, user_id: str) -> UserAuth:
        assert user_id == 'root'
        return DefaultUserAuth()
