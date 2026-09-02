from __future__ import annotations

import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


@unittest.skipUnless(
    importlib.util.find_spec("jwt") and importlib.util.find_spec("cryptography"),
    "OIDC signature tests require the server optional dependencies",
)
class OIDCTests(unittest.TestCase):
    def test_rejects_disallowed_algorithm_and_invalid_signature(self) -> None:
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa

        from wiki_memory.config import MemoryError
        from wiki_memory.oidc import OIDCConfig, OIDCVerifier
        from wiki_memory.team import Role

        issuer = "https://identity.example"
        audience = "wiki-memory"
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
        jwk["kid"] = "test-key"
        verifier = OIDCVerifier(OIDCConfig(issuer=issuer, audience=audience))
        verifier._load_jwks = lambda: {"keys": [jwk]}  # type: ignore[method-assign]
        claims = {
            "sub": "member-1",
            "aud": audience,
            "iss": issuer,
            "exp": int(time.time()) + 60,
            "roles": ["reader"],
            "groups": ["marketing"],
        }
        token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key"})
        principal = verifier.verify(token)
        self.assertEqual(principal.id, "member-1")
        self.assertEqual(principal.roles, frozenset({Role.READER}))
        self.assertEqual(principal.spaces, frozenset({"marketing"}))

        no_expiration = dict(claims)
        no_expiration.pop("exp")
        token_without_expiration = jwt.encode(
            no_expiration,
            private_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        with self.assertRaisesRegex(MemoryError, "Invalid OIDC token"):
            verifier.verify(token_without_expiration)

        empty_subject = {**claims, "sub": ""}
        token_with_empty_subject = jwt.encode(
            empty_subject,
            private_key,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        with self.assertRaisesRegex(MemoryError, "subject claim must be a non-empty string"):
            verifier.verify(token_with_empty_subject)

        forged_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        tampered = jwt.encode(claims, forged_key, algorithm="RS256", headers={"kid": "test-key"})
        with self.assertRaisesRegex(MemoryError, "Invalid OIDC token"):
            verifier.verify(tampered)

        hmac_token = jwt.encode(claims, "synthetic-secret-at-least-32-characters", algorithm="HS256", headers={"kid": "test-key"})
        with self.assertRaisesRegex(MemoryError, "signing algorithm is not allowed"):
            verifier.verify(hmac_token)
