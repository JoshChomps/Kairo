from __future__ import annotations

import pytest

from kairo.kicad.formats import (
    FOOTPRINT,
    SCHEMATIC,
    SYMBOL_LIBRARY,
    UnsupportedFormatError,
    check_format,
)
from kairo.kicad.sexpr import parse


def sch(version: str, generator: str = "10.0") -> str:
    return (
        f'(kicad_sch\n\t(version {version})\n\t(generator "eeschema")\n'
        f'\t(generator_version "{generator}")\n)\n'
    )


@pytest.mark.parametrize("version", [20241209, 20250114, 20260101, 20260306])
def test_known_schematic_versions_are_read(version: int) -> None:
    assert check_format(parse(sch(str(version))), SCHEMATIC) == version


def test_unknown_version_is_refused_naming_the_writer() -> None:
    with pytest.raises(UnsupportedFormatError) as caught:
        check_format(parse(sch("20250610", "9.99")), SCHEMATIC)
    message = str(caught.value)
    assert "20250610" in message
    assert "9.99" in message
    assert "20260306" in message


def test_kicad_8_schematic_is_refused() -> None:
    with pytest.raises(UnsupportedFormatError):
        check_format(parse(sch("20231120", "8.0")), SCHEMATIC)


def test_wrong_kind_is_refused() -> None:
    with pytest.raises(UnsupportedFormatError, match="kicad_symbol_lib"):
        check_format(parse(sch("20260306")), SYMBOL_LIBRARY)


@pytest.mark.parametrize(
    "text",
    [
        "(kicad_sch)",
        '(kicad_sch (version "20260306"))',
        "(kicad_sch (version 2026x))",
        "(kicad_sch (version 1 2))",
        "(a) (b)",
    ],
)
def test_missing_or_odd_version_is_refused(text: str) -> None:
    with pytest.raises(UnsupportedFormatError):
        check_format(parse(text), SCHEMATIC)


def test_footprint_versions() -> None:
    text = '(footprint "R_0603"\n\t(version 20260206)\n)\n'
    assert check_format(parse(text), FOOTPRINT) == 20260206
