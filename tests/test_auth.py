import time
from dataclasses import dataclass

from ado_build_minutes.auth import AZURE_DEVOPS_SCOPE, EntraAuthProvider, redact_secret


@dataclass
class FakeToken:
    token: str
    expires_on: int


class FakeCredential:
    def __init__(self, tokens):
        self.tokens = list(tokens)
        self.calls = []

    def get_token(self, scope, **kwargs):
        self.calls.append((scope, kwargs))
        return self.tokens.pop(0)


def test_entra_authorization_header_contains_bearer_token():
    credential = FakeCredential([FakeToken("known-token-123", int(time.time()) + 3600)])
    provider = EntraAuthProvider(credential=credential)

    header = provider.authorization_header()

    assert header.startswith("Bearer ")
    assert "known-token-123" in header
    assert credential.calls == [(AZURE_DEVOPS_SCOPE, {})]


def test_entra_authorization_header_refreshes_near_expiry():
    now = int(time.time())
    credential = FakeCredential([
        FakeToken("first-token", now + 10),
        FakeToken("second-token", now + 3600),
    ])
    provider = EntraAuthProvider(refresh_margin_seconds=300, credential=credential)

    first = provider.authorization_header()
    second = provider.authorization_header()

    assert "first-token" in first
    assert "second-token" in second
    assert len(credential.calls) == 2


def test_redact_secret_removes_authorization_values():
    assert "known-token" not in redact_secret("Authorization: Bearer known-token")
    assert "pat-token" not in redact_secret("Authorization: Basic pat-token")
