"""Producer bearer tokens. Only hashes are stored in config."""

import hashlib
import hmac
import secrets

_PREFIX = "sha256:"


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    # Tokens are high-entropy random strings, so a plain digest is sufficient.
    return _PREFIX + hashlib.sha256(token.encode()).hexdigest()


def verify_token(token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), token_hash)
