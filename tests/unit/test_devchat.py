import pytest

from pester.devchat import ChatLine, format_message, parse_line


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("", None),
        ("   ", None),
        ("yes", ChatLine(text="yes")),
        (">3 the answer", ChatLine(text="the answer", reply_to=3)),
        ("!3 2", ChatLine(press=(3, 2))),
        ("/skip", ChatLine(text="/skip")),
        (">x nope", ChatLine(text=">x nope")),
    ],
)
def test_parse_line(line: str, expected: ChatLine | None) -> None:
    assert parse_line(line) == expected


def test_format_message_with_options_and_reply() -> None:
    text = format_message({"id": 4, "text": "Water?", "options": ["Yes", "No"], "reply_to": 2})
    assert text == "[#4] pester (re #2): Water?\n      1) Yes   2) No"
