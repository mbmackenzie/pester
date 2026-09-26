import pytest

from pester.cli import main
from pester.core.tokens import hash_token, verify_token


def test_hash_token_given(capsys: pytest.CaptureFixture[str]) -> None:
    main(["hash-token", "secret"])
    assert capsys.readouterr().out.strip() == hash_token("secret")


def test_hash_token_generated(capsys: pytest.CaptureFixture[str]) -> None:
    main(["hash-token"])
    lines = dict(line.split(":", 1) for line in capsys.readouterr().out.splitlines())
    assert verify_token(lines["token"].strip(), lines["hash"].strip())
