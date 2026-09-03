"""Authentication providers for Azure DevOps REST and Analytics APIs."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import logging
import os
import time
from typing import Any, Protocol

try:
    from azure.core.credentials import AccessToken
    from azure.identity import DefaultAzureCredential
except ImportError:  # pragma: no cover - exercised only in dependency-free help/test environments.
    AccessToken = object  # type: ignore[assignment]
    DefaultAzureCredential = None  # type: ignore[assignment]

AZURE_DEVOPS_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"
PAT_ENV_VAR = "AZURE_DEVOPS_EXT_PAT"


class AuthProvider(Protocol):
    """Protocol implemented by supported Azure DevOps auth providers."""

    def authorization_header(self, force_refresh: bool = False) -> str:
        """Return a safe Authorization header value without logging secrets."""
        ...


@dataclass
class EntraAuthProvider:
    """Microsoft Entra ID auth using azure-identity DefaultAzureCredential."""

    tenant_id: str | None = None
    refresh_margin_seconds: int = 300
    credential: Any | None = None

    def __post_init__(self) -> None:
        if self.credential is None and DefaultAzureCredential is None:
            raise RuntimeError("azure-identity is required for Entra auth. Install with: pip install -e '.[runtime]'")
        self._credential = self.credential or DefaultAzureCredential(exclude_interactive_browser_credential=True)
        self._token: AccessToken | None = None
        self._log = logging.getLogger(__name__)

    def authorization_header(self, force_refresh: bool = False) -> str:
        """Return a Bearer auth header, refreshing before the token expires."""
        now = int(time.time())
        if (
            force_refresh
            or self._token is None
            or self._token.expires_on - now <= self.refresh_margin_seconds
        ):
            kwargs = {"tenant_id": self.tenant_id} if self.tenant_id else {}
            self._log.debug("Acquiring Azure DevOps Entra token%s", " for configured tenant" if self.tenant_id else "")
            self._token = self._credential.get_token(AZURE_DEVOPS_SCOPE, **kwargs)
        return f"Bearer {self._token.token}"


@dataclass
class PatAuthProvider:
    """Explicit opt-in PAT fallback using AZURE_DEVOPS_EXT_PAT."""

    env_var: str = PAT_ENV_VAR

    def authorization_header(self, force_refresh: bool = False) -> str:
        """Return a Basic auth header for the PAT from the environment."""
        token = os.environ.get(self.env_var)
        if not token:
            raise RuntimeError(f"PAT auth requested but {self.env_var} is not set")
        encoded = base64.b64encode(f":{token}".encode("utf-8")).decode("ascii")
        return f"Basic {encoded}"


def make_auth_provider(mode: str, tenant_id: str | None = None) -> AuthProvider:
    """Create the requested auth provider."""
    if mode == "entra":
        return EntraAuthProvider(tenant_id=tenant_id)
    if mode == "pat":
        return PatAuthProvider()
    raise ValueError(f"Unsupported auth mode: {mode}")


def redact_secret(text: str | None) -> str:
    """Redact authorization-like values from logs and errors."""
    if not text:
        return ""
    redacted = text
    for marker in ("Bearer ", "Basic "):
        if marker in redacted:
            prefix, _, tail = redacted.partition(marker)
            token = tail.split()[0] if tail.split() else ""
            if token:
                redacted = redacted.replace(marker + token, marker + "******")
    return redacted
