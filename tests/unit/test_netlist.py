from __future__ import annotations

from pathlib import Path

import pytest

from kairo.kicad.netlist import NetlistFormatError, Node, parse_netlist

# A netlist kicad-cli 10.0.6 wrote for the mikoto board in tests/fixtures/real.
MIKOTO = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "kicad10"
    / "netlist"
    / "mikoto.net"
)


def mikoto_text() -> str:
    return MIKOTO.read_bytes().decode("utf-8")


def test_a_real_netlist_is_read_whole() -> None:
    netlist = parse_netlist(mikoto_text())
    assert netlist.tool == "Eeschema 10.0.6"
    assert netlist.sheets == ("/",)
    assert len(netlist.components) == 67
    assert len(netlist.nets) == 93
    refs = [c.ref for c in netlist.components]
    assert len(set(refs)) == len(refs)


def test_a_component_keeps_its_symbol_footprint_and_sheet() -> None:
    netlist = parse_netlist(mikoto_text())
    antenna = netlist.components[0]
    assert (antenna.ref, antenna.value) == ("AE1", "Antenna")
    assert antenna.lib_id == "Device:Antenna"
    assert antenna.footprint == "footprints:2.4GHz-meander-antenna-v2"
    assert antenna.description == "Antenna"
    assert antenna.sheet == "/"


def test_a_net_lists_every_pin_with_its_type() -> None:
    netlist = parse_netlist(mikoto_text())
    net = next(n for n in netlist.nets if n.name == "/ANT")
    assert net.nodes == (
        Node("AE1", "1", "A_1", "input"),
        Node("C22", "2", "", "passive"),
        Node("R11", "2", "", "passive"),
    )


def test_every_node_names_a_component_in_the_netlist() -> None:
    netlist = parse_netlist(mikoto_text())
    refs = {c.ref for c in netlist.components}
    assert {node.ref for net in netlist.nets for node in net.nodes} <= refs


SMALL = """(export (version "E")
  (design (tool "Eeschema 10.0.6") (sheet (number "1") (name "/")))
  (components
    (comp (ref "R1") (value "1k") (footprint)
      (libsource (lib "Device") (part "R") (description))
      (sheetpath (names "/") (tstamps "/"))))
  (nets (net (code "1") (name "N") (node (ref "R1") (pin "1") (pintype "passive")))))"""


def test_fields_written_with_no_value_are_empty() -> None:
    (resistor,) = parse_netlist(SMALL).components
    assert (resistor.footprint, resistor.description) == ("", "")
    assert resistor.fields == ()


def test_a_parts_own_fields_are_kept_when_they_hold_something() -> None:
    netlist = parse_netlist(mikoto_text())
    capacitor = next(c for c in netlist.components if c.ref == "C1")
    held = dict(capacitor.fields)
    assert held["Manufacturer Part"] == "CL05A475KP5NRNC"
    assert held["LCSC Part"].startswith("C")
    # KiCad writes a field with nothing in it too; those are left out.
    antenna = next(c for c in netlist.components if c.ref == "AE1")
    assert "Datasheet" not in dict(antenna.fields)
    crowded = SMALL.replace(
        "(footprint)",
        '(footprint) (fields (field (name "LCSC") "C1" "C2"))',
    )
    with pytest.raises(NetlistFormatError, match="more than one value"):
        parse_netlist(crowded)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('(version "E")', '(version "F")', "version 'F'"),
        ("(export", "(kicad_sch", "top-level list"),
        ('(ref "R1") (value', "(value", "no (ref ...)"),
        ('(pin "1") ', "", "no (pin ...)"),
        ('(pintype "passive")', "(pintype)", "does not hold one value"),
        ('(lib "Device") ', "", "no (lib ...)"),
        ("(nets", "(wires", "no (nets ...)"),
        (")))))", "", "not a netlist"),
    ],
)
def test_a_netlist_laid_out_another_way_is_refused(
    old: str, new: str, message: str
) -> None:
    assert old in SMALL
    with pytest.raises(NetlistFormatError) as caught:
        parse_netlist(SMALL.replace(old, new, 1))
    assert message in str(caught.value)


# How kicad-cli 10.0.7 writes a part drawn as two units (the BC857BS dual
# transistor Q3 of the git4dcc C13 board in tests/fixtures/real): one
# tstamp per unit's symbol.
TWO_UNITS = SMALL.replace(
    '(tstamps "/"))))',
    '(tstamps "/00000000-0000-0000-0000-00005b6d3404/"))'
    ' (tstamps "e57090ff-797e-4ab7-9187-71bac3c57331"'
    ' "2a7a12b5-aa10-478f-8412-34b36f7c10d7")))',
)


def test_a_part_drawn_as_two_units_keeps_both_and_names_no_path() -> None:
    (part,) = parse_netlist(TWO_UNITS).components
    assert part.units == (
        "e57090ff-797e-4ab7-9187-71bac3c57331",
        "2a7a12b5-aa10-478f-8412-34b36f7c10d7",
    )
    # Which unit a footprint's path names is not known, so none is guessed.
    assert part.path == ""


def test_a_part_drawn_as_one_unit_has_its_path() -> None:
    one = TWO_UNITS.replace(' "2a7a12b5-aa10-478f-8412-34b36f7c10d7"', "")
    (part,) = parse_netlist(one).components
    assert part.path == (
        "00000000-0000-0000-0000-00005b6d3404/e57090ff-797e-4ab7-9187-71bac3c57331"
    )


def test_a_board_is_not_made_for_a_part_whose_footprint_path_is_unknown() -> None:
    from kairo.pcb.assemble import parts_from_netlist
    from kairo.pcb.board_writer import BoardError

    two = TWO_UNITS.replace("(footprint)", '(footprint "Package_TO_SOT_SMD:SOT-363")')
    with pytest.raises(BoardError, match="2 units"):
        parts_from_netlist(parse_netlist(two), {"R1": (0.0, 0.0, 0.0)}, "x.kicad_sch")
