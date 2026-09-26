import pytest

from pester.core.messages import Command, parse_command


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/skip", Command(name="skip")),
        ("/Snooze  2h ", Command(name="snooze", args="2h")),
        ("/status", Command(name="status")),
        ("hello", None),
        ("/", None),
        ("/ skip", None),
        ("/2h", None),
        ("a /skip", None),
    ],
)
def test_parse_command(text: str, expected: Command | None) -> None:
    assert parse_command(text) == expected
