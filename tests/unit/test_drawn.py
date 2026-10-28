from __future__ import annotations

from pathlib import Path

import pytest

from kairo.ir import split_pin
from kairo.kicad.drawn import DrawnError, design_name, read_drawn
from kairo.kicad.netlist import Component, Net, Netlist, Node, parse_netlist
from kairo.power import build_tree

# A netlist kicad-cli 10.0.6 wrote for the mikoto board in tests/fixtures/real.
MIKOTO = (
    Path(__file__).parent.parent / "fixtures" / "kicad10" / "netlist" / "mikoto.net"
)


@pytest.fixture(scope="module")
def netlist() -> Netlist:
    return parse_netlist(MIKOTO.read_text(encoding="utf-8"))


def test_every_part_and_connected_pin_is_kept(netlist: Netlist) -> None:
    drawn = read_drawn(netlist, "mikoto")
    design = drawn.grounded.design
    assert set(design.components) == {part.ref for part in netlist.components}
    connected = {
        (node.ref, node.pin, net.name)
        for net in netlist.nets
        for node in net.nodes
        if "no_connect" not in node.pin_type
    }
    assert {(p.ref, p.pin.number, p.net) for p in drawn.grounded.pins} == connected
    assert sum(len(pins) for pins in design.connections.values()) == len(connected)


def test_a_pin_kicad_marks_open_goes_to_no_connect(netlist: Netlist) -> None:
    drawn = read_drawn(netlist, "mikoto")
    marked = {
        f"{node.ref}.{node.pin}"
        for net in netlist.nets
        for node in net.nodes
        if "no_connect" in node.pin_type
    }
    assert marked
    assert set(drawn.grounded.design.no_connect) == marked
    on_nets = {f"{p.ref}.{p.pin.number}" for p in drawn.grounded.pins}
    assert not marked & on_nets


def test_no_role_is_guessed_and_supply_nets_are_listed(netlist: Netlist) -> None:
    drawn = read_drawn(netlist, "mikoto")
    assert {net.role for net in drawn.grounded.design.nets.values()} == {"signal"}
    # GND has supply pins on it, and is still not called ground until the
    # caller says so.
    assert "GND" in drawn.unstated
    assert build_tree(drawn.grounded).rails == {}


def test_stated_roles_reach_the_power_tree(netlist: Netlist) -> None:
    first = read_drawn(netlist, "mikoto")
    rail = next(name for name in first.unstated if name != "GND")
    drawn = read_drawn(
        netlist,
        "mikoto",
        {
            "nets": {
                "GND": {"role": "ground"},
                rail: {"role": "power", "voltage": "3.3V"},
            },
            "loads": [{"net": rail, "current": "10mA", "note": "stated"}],
        },
    )
    assert "GND" not in drawn.unstated and rail not in drawn.unstated
    tree = build_tree(drawn.grounded)
    assert tree.rails[rail].voltage == 3.3
    assert any(draw.current == 0.01 for draw in tree.rails[rail].draws)


@pytest.mark.parametrize(
    "facts",
    [
        {"nets": {"NO_SUCH_NET": {"role": "ground"}}},
        {"components": {"NO_SUCH_PART": {"params": {"i_q": "1mA"}}}},
        {"wires": []},
        {"nets": {"GND": {"role": "power"}}},
        {"loads": [{"net": "NO_SUCH_NET", "current": "1mA"}]},
    ],
)
def test_facts_about_things_the_schematic_lacks_are_refused(
    netlist: Netlist, facts: dict[str, object]
) -> None:
    with pytest.raises(DrawnError) as caught:
        read_drawn(netlist, "mikoto", facts)
    assert caught.value.code == "E_FACTS_INVALID"


def test_the_drawing_owns_a_parts_symbol_and_value(netlist: Netlist) -> None:
    ref = netlist.components[0].ref
    with pytest.raises(DrawnError, match="the drawing gives"):
        read_drawn(netlist, "mikoto", {"components": {ref: {"value": "1k"}}})


def odd_netlist(ref: str, lib: str) -> Netlist:
    return Netlist(
        "test",
        ("/",),
        (
            Component(ref, "100n", "", f"{lib}:C_1", "", "/"),
            Component("C", "1u", "", "Device:C", "", "/"),
        ),
        (
            Net("A", (Node(ref, "1", "", "passive"), Node("C", "1", "", "passive"))),
            Net("B", (Node(ref, "2", "", "passive"), Node("C", "2", "", "passive"))),
        ),
    )


def test_a_reference_with_dots_is_told_from_a_pin_name() -> None:
    # Seen on a real board: parts on a sheet used several times are C.0.0,
    # C.0.1 and so on, beside which a part named C would be mistaken for
    # their owner if the text were split at its first dot.
    drawn = read_drawn(odd_netlist("C.0.1", "Device"), "odd")
    assert drawn.grounded.design.connections == {
        "A": ["C.0.1.1", "C.1"],
        "B": ["C.0.1.2", "C.2"],
    }
    assert split_pin("C.0.1.2", drawn.grounded.design.components) == ("C.0.1", "2")
    assert split_pin("C.2", drawn.grounded.design.components) == ("C", "2")


def test_a_symbol_with_no_library_is_kept_as_kicad_names_it() -> None:
    # Seen on real boards: KiCad's netlist gives an empty library for a
    # symbol whose library link is gone.
    drawn = read_drawn(odd_netlist("C7", ""), "odd")
    assert drawn.grounded.design.components["C7"].lib_id == ":C_1"


def test_a_project_name_becomes_a_design_name() -> None:
    assert design_name("my board (rev 2)") == "my_board_rev_2"
    assert design_name("...") == "drawn"
