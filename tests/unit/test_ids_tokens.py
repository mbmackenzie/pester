from ulid import ULID

from pester.core.ids import new_id
from pester.core.tokens import generate_token, hash_token, verify_token


def test_new_id_is_unique_ulid() -> None:
    ids = {new_id() for _ in range(1000)}
    assert len(ids) == 1000
    for i in ids:
        ULID.from_str(i)


def test_token_round_trip() -> None:
    token = generate_token()
    hashed = hash_token(token)
    assert hashed.startswith("sha256:")
    assert token not in hashed
    assert verify_token(token, hashed)
    assert not verify_token(token + "x", hashed)
