from __future__ import annotations

import json
import time
import urllib.request
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from .config import MemoryError
from .team import Principal, Role


@dataclass
class OIDCConfig:
    issuer: str
    audience: str
    jwks_url: str | None = None
    subject_claim: str = "sub"
    groups_claim: str = "groups"
    roles_claim: str = "roles"
    group_space_map: dict[str, tuple[str, ...]] = field(default_factory=dict)
    cache_seconds: int = 3600
    algorithms: tuple[str, ...] = ("RS256", "RS384", "RS512", "ES256", "ES384", "EdDSA")


class OIDCVerifier:
    @staticmethod
    def _validate_endpoint(label: str, value: str) -> None:
        parsed = urllib.parse.urlparse(value)
        if not parsed.hostname:
            raise MemoryError(f"{label} must be an absolute HTTP(S) URL.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise MemoryError(f"{label} cannot contain credentials, a query, or a fragment.")
        local_http = parsed.scheme == "http" and parsed.hostname in {
            "localhost", "127.0.0.1", "::1"
        }
        if parsed.scheme != "https" and not local_http:
            raise MemoryError(f"{label} must use HTTPS outside local development.")

    def __init__(self, config: OIDCConfig):
        self.config = config
        self._validate_endpoint("OIDC issuer", config.issuer)
        if config.jwks_url:
            self._validate_endpoint("OIDC JWKS URL", config.jwks_url)
        self._jwks: dict[str, Any] | None = None
        self._expires_at = 0.0

    def _metadata(self) -> dict[str, Any]:
        url = self.config.issuer.rstrip("/") + "/.well-known/openid-configuration"
        with urllib.request.urlopen(url, timeout=10) as response:
            return json.loads(response.read())

    def _load_jwks(self) -> dict[str, Any]:
        if self._jwks is not None and time.time() < self._expires_at:
            return self._jwks
        url = self.config.jwks_url or self._metadata()["jwks_uri"]
        self._validate_endpoint("Discovered OIDC JWKS URL", str(url))
        with urllib.request.urlopen(url, timeout=10) as response:
            self._jwks = json.loads(response.read())
        self._expires_at = time.time() + self.config.cache_seconds
        return self._jwks

    def verify(self, token: str) -> Principal:
        try:
            import jwt
        except ImportError as exc:
            raise MemoryError("OIDC authentication requires the 'server' optional dependencies.") from exc
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in self.config.algorithms:
                raise MemoryError(f"OIDC signing algorithm is not allowed: {header.get('alg')}")
            key = next(item for item in self._load_jwks()["keys"] if item.get("kid") == header.get("kid"))
            public_key = jwt.PyJWK.from_dict(key).key
            claims = jwt.decode(
                token,
                public_key,
                algorithms=list(self.config.algorithms),
                audience=self.config.audience,
                issuer=self.config.issuer,
                options={"require": ["exp", self.config.subject_claim]},
            )
        except Exception as exc:
            raise MemoryError(f"Invalid OIDC token: {exc}") from exc
        subject = claims.get(self.config.subject_claim)
        if not isinstance(subject, str) or not subject.strip():
            raise MemoryError("Invalid OIDC token: subject claim must be a non-empty string")
        raw_roles = claims.get(self.config.roles_claim, [])
        if isinstance(raw_roles, str):
            raw_roles = [raw_roles]
        roles = frozenset(Role(item) for item in raw_roles if item in {role.value for role in Role})
        raw_groups = claims.get(self.config.groups_claim, [])
        if isinstance(raw_groups, str):
            raw_groups = [raw_groups]
        groups = frozenset(str(item) for item in raw_groups)
        if self.config.group_space_map:
            spaces = frozenset(
                space
                for group in groups
                for space in self.config.group_space_map.get(group, ())
            )
        else:
            spaces = groups
        return Principal(
            id=subject,
            roles=roles,
            spaces=spaces,
            groups=groups,
            kind="service" if Role.SERVICE in roles else "user",
        )
