from __future__ import annotations

import pytest

from kairo.kicad.project import ProjectFormatError, parse_project

LF = (
    '{\n  "board": {\n    "layers": [\n      1,\n      2.5\n    ]\n  },\n'
    '  "name": "é"\n}\n'
)
CRLF = LF.replace("\n", "\r\n")


@pytest.mark.parametrize("text", [LF, CRLF])
def test_round_trip(text: str) -> None:
    assert parse_project(text).to_text() == text


def test_line_ending_is_kept_after_an_edit() -> None:
    project = parse_project(CRLF)
    project.data["name"] = "x"
    assert project.to_text() == CRLF.replace('"é"', '"x"')


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1}\n',
        '{\n  "a": 1\n}',
        "[\n  1\n]\n",
        "not json",
        '{\r\n  "a": 1\n}\r\n',
    ],
)
def test_refused(text: str) -> None:
    with pytest.raises(ProjectFormatError):
        parse_project(text)
