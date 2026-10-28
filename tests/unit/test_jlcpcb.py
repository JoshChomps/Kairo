"""JLCPCB's assembly files from what KiCad writes."""

from __future__ import annotations

import csv
import io

import pytest

from kairo.pcb.jlcpcb import bill, placement

# As kicad-cli 10.0.7 writes them (pcb export pos --format csv --units mm;
# sch export bom), from Kairo's BMS board.
POS = """Ref,Val,Package,PosX,PosY,Rot,Side
"C1","100nF","C_0603_1608Metric",70.250000,-46.000000,90.000000,top
"R7","10k","R_0603_1608Metric",12.500000,-8.000000,0.000000,bottom
"""
BOM = """"Refs","Value","Footprint","Qty","DNP"
"C1","100nF","Capacitor_SMD:C_0603_1608Metric","1",""
"C2","100nF","Capacitor_SMD:C_0603_1608Metric","1",""
"R7","10k","Resistor_SMD:R_0603_1608Metric","1",""
"U1","BQ76952","Package_QFP:TQFP-48_7x7mm_P0.5mm","1",""
"""


def rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def test_the_placement_file_takes_jlcpcbs_column_names_and_keeps_its_values() -> None:
    assert rows(placement(POS)) == [
        ["Designator", "Mid X", "Mid Y", "Rotation", "Layer"],
        ["C1", "70.250000", "-46.000000", "90.000000", "top"],
        ["R7", "12.500000", "-8.000000", "0.000000", "bottom"],
    ]


def test_a_placement_file_without_a_needed_column_is_refused() -> None:
    with pytest.raises(ValueError, match="lacks Side"):
        placement("Ref,Val,Package,PosX,PosY,Rot\n" + '"C1","1","x",1,2,0\n')


def test_the_bom_lists_each_kind_once_with_its_lcsc_number() -> None:
    numbers = {"CL10B104KB8NNNC": "C1591", "RC0603FR-0710KL": "C98220"}
    text, unsourced = bill(
        BOM,
        {"C1": "CL10B104KB8NNNC", "C2": "CL10B104KB8NNNC", "R7": "RC0603FR-0710KL"},
        numbers.get,
    )
    assert rows(text) == [
        ["Comment", "Designator", "Footprint", "LCSC Part #"],
        ["100nF", "C1,C2", "C_0603_1608Metric", "C1591"],
        ["10k", "R7", "R_0603_1608Metric", "C98220"],
        ["BQ76952", "U1", "TQFP-48_7x7mm_P0.5mm", ""],
    ]
    # A part with no number is named and left empty, never guessed.
    assert unsourced == ["U1 (BQ76952): no part number in the design"]


def test_a_part_number_the_catalogue_lacks_is_named() -> None:
    _, unsourced = bill(BOM, {"R7": "NOT-A-PART"}, lambda _: None)
    assert "R7 (10k): part number NOT-A-PART is not in the catalogue" in unsourced
