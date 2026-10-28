from __future__ import annotations

import pytest

from kairo.kicad.formats import UnsupportedFormatError
from kairo.kicad.sexpr import parse
from kairo.library import (
    Footprint,
    LibraryFormatError,
    Pad,
    Pin,
    PinAlternate,
    read_footprint,
    read_symbol_library,
)

# Written by hand in the layout KiCad 10 uses (see kairo.library.reader), cut
# down to what the reader looks at. The real libraries are read by the tests
# in tests/integration/test_library_real.py.
HEADER = (
    '(version 20251024) (generator "kicad_symbol_editor") (generator_version "10.0")'
)
REG = """
(symbol "REG"
  (property "Reference" "U" (at 0 0 0))
  (property "Value" "REG" (at 0 0 0))
  (property "Footprint" "Pkg:SOT-223" (at 0 0 0))
  (property "Datasheet" "https://example.com/reg.pdf" (at 0 0 0))
  (property "Description" "Test regulator" (at 0 0 0))
  (property "ki_keywords" "ldo test" (at 0 0 0))
  (property "ki_fp_filters" "SOT*" (at 0 0 0))
  (property private "KLC_S4.1" "a note" (at 0 0 0))
  (property private "KLC_S4.1" "a second note under the same key" (at 0 0 0))
  (symbol "REG_0_1" (rectangle (start -5.08 5.08) (end 5.08 -5.08)))
  (symbol "REG_1_1"
    (pin power_in line (at -7.62 0 0) (length 2.54) (name "VI") (number "3"))
    (pin power_out line (at 7.62 0 180) (length 2.54) (name "VO") (number "2")
      (alternate "SENSE" input inverted))
    (pin power_in line (at 0 -7.62 90) (length 2.54) (name "GND") (number "1")
      (hide yes)))
  (symbol "REG_2_1"
    (pin passive line (at 0 0 270) (length 1.27) (name "") (number "4"))))
"""
DERIVED = """
(symbol "REG-A" (extends "REG")
  (property "Value" "REG-A" (at 0 0 0))
  (property "Description" "First variant" (at 0 0 0)))
(symbol "REG-B" (extends "REG-A")
  (property "Value" "REG-B" (at 0 0 0)))
(symbol "GND" (power)
  (property "Reference" "#PWR" (at 0 0 0))
  (symbol "GND_1_1"
    (pin power_in line (at 0 0 270) (length 0) (name "GND") (number "1"))))
"""


def library(*symbols: str, header: str = HEADER) -> str:
    return f"(kicad_symbol_lib {header} {' '.join(symbols)})"


def read(*symbols: str) -> dict[str, object]:
    return {s.name: s for s in read_symbol_library(parse(library(*symbols)))}


def test_pins_are_read_exactly() -> None:
    (reg,) = read_symbol_library(parse(library(REG)))
    assert reg.pins == (
        Pin("3", "VI", "power_in", "line", 1, 1, -7.62, 0.0, 0.0, 2.54, False),
        Pin(
            "2",
            "VO",
            "power_out",
            "line",
            1,
            1,
            7.62,
            0.0,
            180.0,
            2.54,
            False,
            (PinAlternate("SENSE", "input", "inverted"),),
        ),
        Pin("1", "GND", "power_in", "line", 1, 1, 0.0, -7.62, 90.0, 2.54, True),
        Pin("4", "", "passive", "line", 2, 1, 0.0, 0.0, 270.0, 1.27, False),
    )
    assert reg.unit_count == 2
    assert not reg.is_power


def test_properties_are_read_and_private_notes_are_not() -> None:
    (reg,) = read_symbol_library(parse(library(REG)))
    assert (reg.reference, reg.value, reg.footprint) == ("U", "REG", "Pkg:SOT-223")
    assert reg.datasheet == "https://example.com/reg.pdf"
    assert (reg.description, reg.keywords) == ("Test regulator", "ldo test")
    assert reg.footprint_filters == "SOT*"
    assert (reg.extends, reg.body) == (None, "REG")


def test_a_derived_symbol_has_its_own_fields_and_its_ancestors_pins() -> None:
    symbols = {s.name: s for s in read_symbol_library(parse(library(REG, DERIVED)))}
    a, b = symbols["REG-A"], symbols["REG-B"]
    assert (a.extends, a.body) == ("REG", "REG")
    assert (b.extends, b.body) == ("REG-A", "REG")
    assert a.pins == b.pins == symbols["REG"].pins
    assert (a.value, a.description) == ("REG-A", "First variant")
    # A field the derived symbol does not state is left empty, not copied.
    assert b.description == ""
    assert b.unit_count == 2


def test_power_symbols_are_marked() -> None:
    symbols = {s.name: s for s in read_symbol_library(parse(library(REG, DERIVED)))}
    assert symbols["GND"].is_power
    assert symbols["GND"].reference == "#PWR"


PIN = '(pin passive line (at 0 0 0) (length 1.27) (name "") (number "1"))'


@pytest.mark.parametrize(
    ("symbols", "message"),
    [
        ('(symbol "X") (symbol "X")', "appears twice"),
        ('(symbol "X" (extends "Y"))', "not in the library"),
        ('(symbol "X" (extends "Y")) (symbol "Y" (extends "X"))', "loop"),
        (
            f'(symbol "Y" (symbol "Y_1_1" {PIN})) '
            f'(symbol "X" (extends "Y") (symbol "X_1_1" {PIN}))',
            "body of its own",
        ),
        (f'(symbol "X" {PIN})', "outside any unit"),
        (f'(symbol "X" (symbol "X_1" {PIN}))', "<name>_<unit>_<style>"),
        (f'(symbol "X" (symbol "Other_1_1" {PIN}))', "<name>_<unit>_<style>"),
        (
            f'(symbol "X" (symbol "X_1_1" {PIN.replace("passive", "clever")}))',
            "unknown electrical type 'clever'",
        ),
        (
            f'(symbol "X" (symbol "X_1_1" {PIN.replace("(at 0 0 0)", "(at 0 0)")}))',
            "no (at x y angle)",
        ),
        (
            f'(symbol "X" (symbol "X_1_1" {PIN.replace("(at 0", "(at nan")}))',
            "'nan' is not a number",
        ),
        (
            f'(symbol "X" (symbol "X_1_1" {PIN.replace("1.27", "1_27")}))',
            "'1_27' is not a number",
        ),
        (
            f'(symbol "X" (symbol "X_1_1" {PIN.replace("(length 1.27)", "")}))',
            "no (length ...)",
        ),
        (
            f'(symbol "X" (symbol "X_1_1" {PIN[:-1] + " (hide maybe))"}))',
            "hide",
        ),
        (
            '(symbol "X" (property "Value" "a") (property "Value" "b"))',
            "two 'Value' properties",
        ),
        ('(symbol "X" (property Value "a"))', "expected a quoted string"),
    ],
)
def test_a_symbol_laid_out_another_way_is_refused(symbols: str, message: str) -> None:
    with pytest.raises(LibraryFormatError) as caught:
        read_symbol_library(parse(library(symbols)))
    assert message in str(caught.value)


def test_an_unknown_library_format_is_refused() -> None:
    with pytest.raises(UnsupportedFormatError):
        read_symbol_library(parse(library(REG, header="(version 20991231)")))


FOOTPRINT = """(footprint "R_0603"
  (version 20260206) (generator "pcbnew") (layer "F.Cu")
  (descr "Resistor, 0603") (tags "resistor")
  (pad "1" smd roundrect (at -0.8 0) (size 0.8 0.95))
  (pad "2" smd roundrect (at 0.8 0) (size 0.8 0.95))
  (pad "" np_thru_hole circle (at 0 0) (size 1 1)))"""


def test_a_footprints_pads_are_read_in_order() -> None:
    assert read_footprint(parse(FOOTPRINT)) == Footprint(
        "R_0603",
        "Resistor, 0603",
        "resistor",
        (
            Pad("1", "smd", "roundrect"),
            Pad("2", "smd", "roundrect"),
            Pad("", "np_thru_hole", "circle"),
        ),
    )


def test_a_footprint_without_description_or_tags_has_empty_ones() -> None:
    text = '(footprint "X" (version 20260206) (pad "1" smd rect))'
    assert read_footprint(parse(text)) == Footprint(
        "X", "", "", (Pad("1", "smd", "rect"),)
    )


def test_a_pad_laid_out_another_way_is_refused() -> None:
    with pytest.raises(LibraryFormatError):
        read_footprint(parse('(footprint "X" (version 20260206) (pad 1 smd rect))'))
