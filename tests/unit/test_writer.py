# These tests change plain JSON data with small lambdas, which a strict type
# checker cannot follow; the checks that follow do not help here.
# pyright: reportUnknownLambdaType=false, reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from minilib import ldo, make_index

from kairo.ir import Design
from kairo.kicad.formats import SCHEMATIC, check_format
from kairo.kicad.geometry import (
    PLACEMENTS,
    Mirror,
    Placement,
    Rotation,
    pin_outward,
    snap,
    to_sheet,
)
from kairo.kicad.netlist import Component, Net, Netlist, Node
from kairo.kicad.schematic_writer import WriterError, write_schematic
from kairo.kicad.sexpr import parse
from kairo.library import LibraryIndex
from kairo.library.embed import embedded_symbol
from kairo.library.grounding import Grounded, ground
from kairo.library.reader import LibraryFormatError
from kairo.verify.equivalence import compare_netlist


@pytest.fixture
def index(tmp_path: Path) -> LibraryIndex:
    return make_index(tmp_path)


@pytest.fixture
def grounded(index: LibraryIndex) -> Grounded:
    return ground(Design.model_validate(ldo()), index)


# Geometry


def test_a_pin_lands_where_each_of_kicads_eight_placements_puts_it() -> None:
    # A pin at (2.54, 1.27) in the library, which draws y upward.
    want: dict[tuple[Rotation, Mirror], tuple[float, float]] = {
        (0, ""): (102.54, 48.73),
        (90, ""): (98.73, 47.46),
        (180, ""): (97.46, 51.27),
        (270, ""): (101.27, 52.54),
        (0, "x"): (102.54, 51.27),
        (0, "y"): (97.46, 48.73),
        (90, "x"): (98.73, 52.54),
        (270, "x"): (101.27, 47.46),
    }
    assert set(want) == set(PLACEMENTS)
    for (rotation, mirror), point in want.items():
        assert to_sheet(Placement(100, 50, rotation, mirror), 2.54, 1.27) == point


def test_a_stub_leaves_a_pin_away_from_the_body() -> None:
    # Angle 0 in the library: the pin is drawn rightward from its end, so
    # the body is to the right and a wire leaves to the left.
    assert pin_outward(Placement(0, 0), 0) == (-1, 0)
    assert pin_outward(Placement(0, 0), 180) == (1, 0)
    assert pin_outward(Placement(0, 0), 90) == (0, 1)
    assert pin_outward(Placement(0, 0), 270) == (0, -1)
    assert pin_outward(Placement(0, 0, 90), 0) == (0, 1)
    assert pin_outward(Placement(0, 0, 0, "y"), 0) == (1, 0)
    with pytest.raises(ValueError, match="45"):
        pin_outward(Placement(0, 0), 45)


def test_snapping_goes_to_the_nearest_grid_point() -> None:
    assert [snap(v) for v in (0, 1.0, 1.9, 50.8, 51.5)] == [0, 1.27, 1.27, 50.8, 52.07]


# Embedding


def test_a_derived_symbol_is_embedded_flattened_under_its_own_name(
    tmp_path: Path, index: LibraryIndex
) -> None:
    path = index.symbol_file("Reg:REG-3.3")
    assert path is not None and path.name == "Reg.kicad_sym"
    library = parse(path.read_text(encoding="utf-8")).root
    before = path.read_text(encoding="utf-8")
    embedded = embedded_symbol(library, "REG-3.3", "Reg")
    assert embedded.items[1].value == "Reg:REG-3.3"  # pyright: ignore[reportAttributeAccessIssue]
    assert [u.items[1].value for u in embedded.lists("symbol")] == [  # pyright: ignore[reportAttributeAccessIssue]
        "REG-3.3_0_1",
        "REG-3.3_1_1",
    ]
    values = {p.values()[0]: p.values()[1] for p in embedded.lists("property")}
    # Its own fields over the base symbol's, and the base's for the ones
    # it does not set: KiCad's rule check expects a derived symbol without
    # keywords to carry its base's.
    assert values["Value"] == "REG-3.3"
    assert values["Footprint"] == "Pkg:SOT-223"
    base = embedded_symbol(library, "REG", "Reg")
    assert values["ki_keywords"] == next(
        p.values()[1] for p in base.lists("property") if p.values()[0] == "ki_keywords"
    )
    assert embedded.first("extends") is None
    assert embedded.first("in_bom") is not None
    # The library's own tree is untouched.
    from kairo.kicad.sexpr import to_text

    assert to_text(library) == before
    with pytest.raises(LibraryFormatError, match="NOPE"):
        embedded_symbol(library, "NOPE", "Reg")


def test_a_derived_symbol_keeps_its_bases_unit_lock() -> None:
    # As in KiCad's Amplifier_Operational library, where LM358 extends
    # LM2904 and only LM2904 says its units are locked. Without the lock
    # KiCad's rule check calls the copy different from the library.
    library = parse(
        '(kicad_symbol_lib (symbol "BASE" (in_bom yes)'
        ' (property "Reference" "U") (property "Value" "BASE")'
        ' (property "ki_locked" "") (property "ki_keywords" "dual")'
        ' (symbol "BASE_1_1") (symbol "BASE_2_1"))'
        ' (symbol "DERIVED" (extends "BASE")'
        ' (property "Reference" "U") (property "Value" "DERIVED")'
        ' (property "ki_keywords" "dual")))'
    ).root
    embedded = embedded_symbol(library, "DERIVED", "Lib")
    names = [p.values()[0] for p in embedded.lists("property")]
    assert names == ["Reference", "Value", "ki_locked", "ki_keywords"]
    assert [
        p.values()[0] for p in embedded_symbol(library, "BASE", "Lib").lists("property")
    ] == ["Reference", "Value", "ki_locked", "ki_keywords"]


def test_a_derived_symbol_keeps_every_field_its_base_sets_and_it_does_not() -> None:
    # As in KiCad's Diode library, where 1N4148W extends 1N4001 and only
    # 1N4001 has the simulation fields, and in Analog_ADC, where the base
    # has a private field. KiCad 10 embeds them all, in the base's order
    # before the derived symbol's ki_ entries; without them its rule check
    # calls the copy different from the library.
    library = parse(
        '(kicad_symbol_lib (symbol "BASE" (in_bom yes)'
        ' (property "Reference" "D") (property "Value" "BASE")'
        ' (property "Sim.Device" "D") (property "Sim.Pins" "1=K 2=A")'
        ' (property private "KLC_S4.4" "pins") (property "ki_keywords" "diode")'
        ' (symbol "BASE_1_1"))'
        ' (symbol "DERIVED" (extends "BASE")'
        ' (property "Reference" "D") (property "Value" "DERIVED")'
        ' (property private "Note" "own") (property "ki_keywords" "fast")'
        ' (property "ki_fp_filters" "SOD*")))'
    ).root
    embedded = embedded_symbol(library, "DERIVED", "Lib")
    assert [p.values()[:2] for p in embedded.lists("property")] == [
        ["Reference", "D"],
        ["Value", "DERIVED"],
        ["private", "Note"],
        ["Sim.Device", "D"],
        ["Sim.Pins", "1=K 2=A"],
        ["private", "KLC_S4.4"],
        ["ki_keywords", "fast"],
        ["ki_fp_filters", "SOD*"],
    ]


# The writer


def test_the_schematic_is_the_same_every_time_and_reads_as_kicad_10(
    grounded: Grounded, index: LibraryIndex
) -> None:
    first = write_schematic(grounded, index, newline="\n")
    again = write_schematic(
        ground(Design.model_validate(ldo()), index), index, newline="\n"
    )
    assert first.text == again.text
    document = parse(first.text)
    assert check_format(document, SCHEMATIC) == 20260306
    root = document.root
    assert [s.items[1].value for s in root.first("lib_symbols").lists("symbol")] == [  # pyright: ignore[reportOptionalMemberAccess, reportAttributeAccessIssue]
        "Device:C",
        "Device:Conn",
        "Device:LED",
        "Device:R",
        "Reg:REG-3.3",
        "power:+3V3",
        "power:GND",
        "power:PWR_FLAG",
    ]
    # One stub for each of the 13 pins, and a second for the two pins that
    # carry a power flag.
    assert len(root.lists("wire")) == 15
    assert sorted(n.values()[0] for n in root.lists("label")) == ["LED_A", "LED_A"]
    assert {n.values()[0] for n in root.lists("global_label")} == {"VIN_5V"}
    references = sorted(
        p.values()[1]
        for s in root.lists("symbol")
        for p in s.lists("property")
        if p.values()[0] == "Reference"
    )
    assert references[:4] == ["#FLG01", "#FLG02", "#PWR01", "#PWR02"]
    assert references[-6:] == ["C1", "C2", "D1", "J1", "R1", "U1"]
    assert first.net_names == {
        "VIN_5V": "VIN_5V",
        "+3V3": "+3V3",
        "LED_A": "/LED_A",
        "GND": "GND",
    }
    # Every identifier is distinct.
    ids = [n.values()[0] for n in _all(root, "uuid")]
    assert len(ids) == len(set(ids))


def _all(node: Any, head: str) -> list[Any]:
    found = []
    for child in node.lists():
        if child.head == head:
            found.append(child)
        found += _all(child, head)
    return found


def test_line_endings_follow_what_is_asked(
    grounded: Grounded, index: LibraryIndex
) -> None:
    unix = write_schematic(grounded, index, newline="\n").text
    windows = write_schematic(grounded, index, newline="\r\n").text
    assert "\r" not in unix and unix.endswith(")\n")
    assert windows == unix.replace("\n", "\r\n")


def test_a_power_net_that_nothing_drives_gets_one_flag(
    grounded: Grounded, index: LibraryIndex
) -> None:
    root = parse(write_schematic(grounded, index, newline="\n").text).root
    flags = [
        s
        for s in root.lists("symbol")
        if s.first("lib_id").values() == ["power:PWR_FLAG"]  # pyright: ignore[reportOptionalMemberAccess]
    ]
    # VIN_5V and GND have power inputs and no power output; +3V3 is driven
    # by the regulator's output and needs none.
    assert len(flags) == 2
    # A flag sits part way along its pin's wire, not on the label at the end.
    ends = {
        tuple(n.first("at").values()[:2])  # pyright: ignore[reportOptionalMemberAccess]
        for n in (*root.lists("label"), *root.lists("global_label"))
    }
    for flag in flags:
        assert tuple(flag.first("at").values()[:2]) not in ends  # pyright: ignore[reportOptionalMemberAccess]


def facing(first: str, second: str) -> dict[str, Any]:
    """A two-pin connector with ``first`` on its upper pin and ``second`` below."""
    return {
        "design": "facing",
        "nets": {
            "+5V": {"role": "power", "voltage": "5V"},
            "GNDA": {"role": "ground"},
        },
        "components": {
            "J1": {"lib_id": "Device:Conn"},
            "C1": {"lib_id": "Device:C", "value": "100n"},
        },
        "connections": {
            first: ["J1.Pin_1", "C1.1"],
            second: ["J1.Pin_2", "C1.2"],
        },
    }


def power_symbols(text: str) -> dict[str, tuple[int, int]]:
    """Each power symbol on the connector's pins: its turn and its name's angle."""
    found: dict[str, tuple[int, int]] = {}
    root = parse(text).root
    for symbol in root.lists("symbol"):
        lib_id = symbol.first("lib_id").values()[0]  # pyright: ignore[reportOptionalMemberAccess]
        x = float(symbol.first("at").values()[0])  # pyright: ignore[reportOptionalMemberAccess]
        if not lib_id.startswith("power:") or lib_id == "power:PWR_FLAG":
            continue
        value = next(p for p in symbol.lists("property") if p.values()[0] == "Value")
        # The connector's stubs run right, past the capacitor's column.
        connector = float(
            next(
                s.first("at").values()[0]  # pyright: ignore[reportOptionalMemberAccess]
                for s in root.lists("symbol")
                if s.first("lib_id").values() == ["Device:Conn"]  # pyright: ignore[reportOptionalMemberAccess]
            )
        )
        if x > connector:
            found[lib_id] = (
                int(symbol.first("at").values()[2]),  # pyright: ignore[reportOptionalMemberAccess]
                int(float(value.first("at").values()[2])),  # pyright: ignore[reportOptionalMemberAccess]
            )
    return found


def test_power_symbols_that_would_face_each_other_are_turned_along_their_stubs(
    index: LibraryIndex,
) -> None:
    # A ground hangs down and a supply stands up (the test library's GNDA
    # and +5V are drawn so, as KiCad's are). On the connector's upper
    # pin a ground faces the lower pin, and a supply there faces back, so
    # upright they would be drawn on top of each other.
    written = write_schematic(
        ground(Design.model_validate(facing("GNDA", "+5V")), index), index
    )
    assert written.findings == ()
    # Both point right, along their stubs: a ground turned 90 degrees, a
    # supply 270. KiCad draws a name at its own angle plus the symbol's,
    # so 90 is written for a name that reads level.
    assert power_symbols(written.text) == {
        "power:GNDA": (90, 90),
        "power:+5V": (270, 90),
    }
    # The other way round, each faces away from the other and stays upright.
    upright = write_schematic(
        ground(Design.model_validate(facing("+5V", "GNDA")), index), index
    )
    assert upright.findings == ()
    assert power_symbols(upright.text) == {
        "power:GNDA": (0, 0),
        "power:+5V": (0, 0),
    }


def test_every_shown_text_gets_a_clear_spot(
    grounded: Grounded, index: LibraryIndex
) -> None:
    written = write_schematic(grounded, index, newline="\n")
    assert written.findings == ()
    score = written.text_score
    assert (score.overlapping, score.ambiguous, score.unplaced) == (0, 0, 0)
    assert (score.too_close, score.too_far) == (0, 0)
    root = parse(written.text).root
    shown = [
        (prop.values()[1], prop.first("at").values())  # pyright: ignore[reportOptionalMemberAccess]
        for symbol in root.lists("symbol")
        for prop in symbol.lists("property")
        if prop.values()[0] in ("Reference", "Value") and prop.first("hide") is None
    ]
    # Six references, six values, and the names of eight power symbols and
    # two flags, each at a place of its own.
    assert len(shown) == 6 + 6 + 8 + 2
    assert len({tuple(at[:2]) for _, at in shown}) == len(shown)
    # The two flags are on different parts.
    flags = [
        symbol.first("at").values()[:2]  # pyright: ignore[reportOptionalMemberAccess]
        for symbol in root.lists("symbol")
        if symbol.first("lib_id").values() == ["power:PWR_FLAG"]  # pyright: ignore[reportOptionalMemberAccess]
    ]
    assert abs(float(flags[0][0]) - float(flags[1][0])) > 20


def test_a_text_with_no_clear_spot_is_hidden_and_reported(
    grounded: Grounded, index: LibraryIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kairo.kicad.field_placement as field_placement
    from kairo.text_placement import Rules

    # With no spot offered beyond the symbol's own outline and none allowed
    # near anything, some text cannot be placed whatever the spacing.
    monkeypatch.setattr(
        field_placement,
        "RULES",
        Rules(gap=0.0, clearance=30.0, step=50.0, pushes=(0.0,), max_distance=0.1),
    )
    written = write_schematic(grounded, index, newline="\n")
    assert written.findings
    assert {f.code for f in written.findings} == {"W_TEXT_NO_SPOT"}
    assert all(f.severity == "warning" and f.layer == "L2" for f in written.findings)
    assert written.text_score.unplaced == len(written.findings)
    # A text with no spot is hidden, never drawn where it would collide.
    root = parse(written.text).root
    hidden = sum(
        1
        for symbol in root.lists("symbol")
        for prop in symbol.lists("property")
        if prop.values()[0] in ("Reference", "Value")
        and not prop.values()[1].startswith("#")
        and prop.first("hide") is not None
    )
    assert hidden == len(written.findings)


def shown_text(text: str) -> dict[tuple[str, str], tuple[str, float, float, float]]:
    """Each shown reference and value: its words and where it is."""
    found: dict[tuple[str, str], tuple[str, float, float, float]] = {}
    for symbol in parse(text).root.lists("symbol"):
        properties = {p.values()[0]: p for p in symbol.lists("property")}
        owner = symbol.first("uuid").values()[0]  # pyright: ignore[reportOptionalMemberAccess]
        for name in ("Reference", "Value"):
            prop = properties[name]
            if prop.first("hide") is None:
                x, y, angle = (float(v) for v in prop.first("at").values())  # pyright: ignore[reportOptionalMemberAccess]
                found[(owner, name)] = (prop.values()[1], x, y, angle)
    return found


def test_an_added_part_moves_no_text_of_the_parts_already_there(
    grounded: Grounded, index: LibraryIndex
) -> None:
    before = shown_text(write_schematic(grounded, index, newline="\n").text)
    data = ldo()
    data["components"]["R2"] = {"lib_id": "Device:R", "value": "10k"}
    data["connections"]["+3V3"].append("R2.1")
    data["connections"]["GND"].append("R2.2")
    data["blocks"]["extra"] = ["R2"]
    data["placement"]["order"].append("extra")
    after = shown_text(
        write_schematic(
            ground(Design.model_validate(data), index), index, newline="\n"
        ).text
    )
    assert len(after) == len(before) + 4
    for key, placed in before.items():
        assert after[key] == placed


def test_text_is_never_stacked_on_a_crowded_sheet(index: LibraryIndex) -> None:
    from kairo.text_placement.kicad_text import text_height, text_length

    written = write_schematic(
        ground(Design.model_validate(more_capacitors(30)), index), index, newline="\n"
    )
    assert written.text_score.overlapping == 0
    boxes = []
    for words, x, y, angle in shown_text(written.text).values():
        width, height = text_length(words), text_height(words)
        if angle:
            width, height = height, width
        boxes.append((x - width / 2, y - height / 2, x + width / 2, y + height / 2))
    assert len(boxes) > 100
    for n, a in enumerate(boxes):
        for b in boxes[n + 1 :]:
            assert not (a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3])


def test_parts_are_given_room_for_their_stubs_and_labels(
    grounded: Grounded, index: LibraryIndex
) -> None:
    written = write_schematic(grounded, index, newline="\n")
    assert written.paper == "A4"
    # Blocks read left to right in the order the design gives. Inside the
    # regulator's block its output capacitor sits beside the output pin,
    # to its right and level with it; the other blocks have no part of
    # more than two pins, so their parts stand in a column.
    x = {ref: placement.x for ref, placement in written.placements.items()}
    assert x["J1"] == x["C1"] < x["U1"] < x["C2"] < x["R1"] == x["D1"]
    y = {ref: placement.y for ref, placement in written.placements.items()}
    assert y["J1"] < y["C1"] and y["R1"] < y["D1"]
    # Beside means the two bodies share some height: the capacitor is
    # level with the pin as far as it can be without rising above the
    # regulator's own room.
    body = written.drawing.bodies
    assert body["C2"].top < body["U1"].bottom and body["C2"].bottom > body["U1"].top
    # Everything is on the 1.27 mm grid.
    for placement in written.placements.values():
        assert round(placement.x / 1.27, 6) == round(placement.x / 1.27)
        assert round(placement.y / 1.27, 6) == round(placement.y / 1.27)
    # Each pin's wire has its own points: 13 pin ends, 13 stub ends, and
    # the far ends of the two flagged stubs.
    root = parse(written.text).root
    points = {
        tuple(xy.values())
        for wire in root.lists("wire")
        for pts in wire.lists("pts")
        for xy in pts.lists("xy")
    }
    assert len(points) == 13 + 13 + 2


def more_capacitors(count: int) -> dict[str, Any]:
    data = ldo()
    for n in range(3, 3 + count):
        data["components"][f"C{n}"] = {"lib_id": "Device:C", "value": "100n"}
        data["connections"]["+3V3"].append(f"C{n}.1")
        data["connections"]["GND"].append(f"C{n}.2")
        data["blocks"]["regulator"].append(f"C{n}")
    return data


def test_a_block_with_many_parts_wraps_and_a_large_design_gets_a_larger_sheet(
    index: LibraryIndex,
) -> None:
    # Twelve more capacitors in one block still fit A4, because the block
    # continues in further columns.
    some = write_schematic(
        ground(Design.model_validate(more_capacitors(12)), index), index
    )
    assert some.paper == "A4"
    assert len({placement.x for placement in some.placements.values()}) > 3
    # Eighty more do not, and the sheet grows. Since the output capacitor
    # sits beside the regulator, its column is one wider: A1 rather than A2.
    many = write_schematic(
        ground(Design.model_validate(more_capacitors(80)), index), index
    )
    assert many.paper in ("A3", "A2", "A1")
    assert f'(paper "{many.paper}")' in many.text
    # No two parts are given the same spot, on either sheet.
    for written in (some, many):
        spots = [(p.x, p.y) for p in written.placements.values()]
        assert len(spots) == len(set(spots))


def test_writing_for_kicad_9_is_refused_until_it_is_checked(
    index: LibraryIndex,
) -> None:
    data = ldo()
    data["kicad_version"] = "9"
    with pytest.raises(WriterError, match="KiCad 9"):
        write_schematic(ground(Design.model_validate(data), index), index)


# Comparing with KiCad's netlist


def netlist_of(grounded: Grounded, names: dict[str, str]) -> Netlist:
    components = tuple(
        Component(ref, "", "", "", "", "/") for ref in grounded.design.components
    )
    nets = tuple(
        Net(
            names[net],
            tuple(
                Node(p.ref, p.pin.number, p.pin.name, "")
                for p in grounded.net_pins(net)
            ),
        )
        for net in grounded.design.connections
    )
    return Netlist("Eeschema 10.0.6", ("/",), components, nets)


def test_the_same_circuit_gives_no_findings(
    grounded: Grounded, index: LibraryIndex
) -> None:
    names = write_schematic(grounded, index).net_names
    assert compare_netlist(grounded, names, netlist_of(grounded, names)) == []


def test_each_kind_of_difference_is_found_and_named(
    grounded: Grounded, index: LibraryIndex
) -> None:
    names = write_schematic(grounded, index).net_names
    good = netlist_of(grounded, names)

    def nets_with(change: Any) -> Netlist:
        nets = {net.name: list(net.nodes) for net in good.nets}
        change(nets)
        return Netlist(
            good.tool,
            good.sheets,
            good.components,
            tuple(Net(name, tuple(nodes)) for name, nodes in nets.items()),
        )

    moved = nets_with(lambda n: n["GND"].append(n["/LED_A"].pop()))
    (finding,) = compare_netlist(grounded, names, moved)
    assert finding.code == "E_PIN_NET_DIFFERS"
    assert (finding.where.ref, finding.where.pin, finding.where.net) == (
        "D1",
        "2",
        "LED_A",
    )

    dropped = nets_with(lambda n: n["VIN_5V"].pop())
    (finding,) = compare_netlist(grounded, names, dropped)
    assert finding.code == "E_PIN_NET_DIFFERS" and "no net" in finding.message

    fewer = Netlist(good.tool, good.sheets, good.components[1:], good.nets)
    assert [f.code for f in compare_netlist(grounded, names, fewer)] == [
        "E_PART_MISSING"
    ]
    more = Netlist(
        good.tool,
        good.sheets,
        (*good.components, Component("R9", "", "", "", "", "/")),
        good.nets,
    )
    assert [f.code for f in compare_netlist(grounded, names, more)] == ["E_PART_EXTRA"]

    renamed = dict(names, LED_A="/LED_B")
    assert {f.where.ref for f in compare_netlist(grounded, renamed, good)} == {
        "R1",
        "D1",
    }


def test_a_spread_net_is_joined_across_blocks_only_where_their_pins_meet() -> None:
    from kairo.kicad import schematic_writer as sw

    private: Any = sw  # the grouping is tested apart from a whole drawing

    def stub(tag: str, x: float, y: float) -> Any:
        ending = private._Ending("N", "", False, False)
        return private._Stub(
            tag, private._Path(((x, y), (x + 2.54, y)), (1, 0)), ending
        )

    block_of = {"R1": "a", "R2": "a", "R3": "b", "R4": "c"}
    # R1 and R2 share block a however far apart; R3 in block b lies within
    # reach of R2; R4 in block c is far from all.
    stubs = [
        stub("R1.1", 0, 0),
        stub("R2.1", 200, 0),
        stub("R3.1", 200 + private._BRIDGE - 10, 5),
        stub("R4.1", 600, 400),
    ]
    groups = private._bridged(stubs, block_of)
    assert [[s.tag for s in g] for g in groups] == [["R1.1", "R2.1", "R3.1"], ["R4.1"]]
    stubs[2] = stub("R3.1", 200 + private._BRIDGE + 10, 5)
    assert len(private._bridged(stubs, block_of)) == 3
